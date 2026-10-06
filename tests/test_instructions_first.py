# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Gerardo Bodegas Martinez
"""An assignment's details say what the work is before they ask about it: the school's
instructions first, whole and each with where it came from, then the notes and the way to
help, then her update and Turning it in, then the rest of the record. A jump at the top
reaches the update however long the instructions are, and a card's Read instructions link
lands on them.

The fixture week through the app, a pinned clock, and forms read from the page's own HTML.
The words are pinned as the pages say them, for her, for a parent, and with the sign-in
off. Nothing here needs a script, and no model is asked.
"""

import json
import pathlib
import re
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from datetime import date
from html import unescape
from urllib.parse import quote, urlsplit

import pytest
from fastapi.testclient import TestClient
from markupsafe import escape

from blossom.hand_in import NEEDS_HAND_IN, TURNED_IN
from blossom.reconciliation import SourceChannel
from blossom.routes import student as student_routes
from blossom.school_instructions import (
    InstructionChoice,
    InstructionSeen,
    InstructionsStanding,
    SchoolInstruction,
)
from blossom.settings import REPOSITORY_ROOT
from blossom.stores.captures import CaptureReadings
from blossom.stores.project_state import HandInReadings, ProjectStateStore, UnreadableClaim
from blossom.views import school_words
from tests.support import (
    ARRIVAL_CUE,
    DETAILS,
    ESSAY,
    ESSAY_ID,
    ESSAY_TITLE,
    FIXTURE_WEEK,
    HER_PAGE,
    HERS,
    NONE_APPLIES,
    NOW,
    PAGE_HEADERS,
    PROBLEM_CUE,
    QUIZ_ID,
    REPORT,
    THEIRS,
    UNDO,
    Answer,
    browser,
    card_for,
    client_for,
    declared_for,
    form_fields,
    homework_from_a_note,
    landing_in,
    planned,
    school_missing,
    signed_in,
    signed_in_household,
    store_of,
    walkthrough,
    week_card,
    whole_form,
)
from tests.support import failed_write as week_refused_by_the_file
from tests.support import malformed as week_malformed
from tests.support import no_choice as week_no_choice
from tests.support import save as week_save

ACTIONS = f"/student/actions/assignments/{ESSAY_ID}"
HAND_IN = f"{ACTIONS}/hand-in"
UNDO_HAND_IN = f"{ACTIONS}/undo-hand-in"
NOWHERE = "assignment-nowhere"
TODAY = date(2026, 8, 19)
READERS = ("her", "a parent", "sign-in off")

A = "Outline three causes before drafting."
B = "Compare two canals in the conclusion."
C = "Bring the map handout."
D = "Cite two of the readings."
LEGACY = "Unreviewed words from before."
LONG = ("Read the chapter on the canal era and answer every question in full. " * 600)[
    :40_000
].strip()

JUMP = '<p class="jump"><a href="#update-or-turn-in">Your update and hand-in status</a></p>'
HER_JUMP = '<p class="jump"><a href="#update-or-turn-in">Her update and hand-in status</a></p>'
HEADING = (
    '<h2 class="update-heading" id="instructions" tabindex="-1">Instructions from the school</h2>'
)
UPDATE_HEADING = '<h2 class="update-heading" id="update-or-turn-in" tabindex="-1">Your update</h2>'
STUDENT_HEADING = (
    '<h2 class="update-heading" id="update-or-turn-in" tabindex="-1">Student update</h2>'
)
UNAVAILABLE = (
    '<p class="source">The school\'s instructions for this assignment cannot be read right '
    "now, so they are not shown.</p>"
)
UNREADABLE_BESIDE_A_NOTE = (
    '<p class="source">Blossom can\'t read the instruction list right now. The older school '
    "note below has not been reviewed.</p>"
)
"""What the details say when the kept instructions cannot be read and a school note left in
the old note field can: the list is what failed, and the note below is still unreviewed."""
ONE_WAITS = (
    '<p class="source">1 instruction from the school is waiting for a parent\'s review. It '
    'does not apply until then. <a href="#waiting-instructions">Read the waiting instruction'
    "</a></p>"
)
TWO_WAIT = (
    '<p class="source">2 instructions from the school are waiting for a parent\'s review. They '
    'do not apply until then. <a href="#waiting-instructions">Read the waiting instructions'
    "</a></p>"
)
WAITING_HEADING = (
    '<h3 class="update-heading" id="waiting-instructions" tabindex="-1">'
    "Waiting for a parent's review</h3>"
)
FOLD = "<summary>Earlier instructions from the school</summary>"
CARD_FOLD = "<summary>Instructions from the school waiting for review, and earlier ones</summary>"
HER_HELP = (
    '<p class="support-links"><a href="/student/due-this-week#ask-for-help">'
    "Ask a parent for help</a></p>"
)
NOT_ATTACHED = (
    "This opens Help on My week. The request won't include this assignment, so name it in "
    "your note if you like."
)
THEIR_HELP = (
    '<p class="support-links"><a href="/parent#help-she-asked-for">Help she asked for</a></p>'
)
NOTES = '<h2 class="update-heading">Notes</h2>'


def quoted(words: str) -> str:
    """Words as the pages quote the school's: escaped, inside their authored-text quote."""
    return f'<q class="authored-text">{escape(words)}</q>'


def applying(label: str, words: str) -> str:
    """One instruction that applies, as the details say it, with where it came from."""
    return f'<p class="from-teacher">From the {label}: {quoted(words)}</p>'


def waiting_item(label: str, words: str) -> str:
    """One kept instruction waiting for a parent's review, in the details' waiting list."""
    return (
        f"<li>From the {label}, waiting for a parent's review: {quoted(words)} "
        '<span class="source">It does not apply until then.</span></li>'
    )


def earlier_item(label: str, words: str) -> str:
    """One instruction said before, in the details' fold."""
    return (
        f"<li>From the {label}, earlier: {quoted(words)} "
        '<span class="source">It does not apply now.</span></li>'
    )


def old_note_item(words: str, mark: str | None) -> str:
    """A school note left in the old note field, as the details' waiting list says it."""
    where = {None: "", "LMS": ", from the school portal", "EMAIL": ", from the school email"}
    return (
        f"<li>Found in the old note field{where[mark]}, not yet reviewed: {quoted(words)} "
        '<span class="source">It is the school\'s, and it does not apply until a parent reviews '
        "it.</span></li>"
    )


# ------------------------------------------------------------- setting the record


@contextmanager
def reading(reader: str, tmp_path: pathlib.Path, *, key: bool = False) -> Iterator[TestClient]:
    """The pinned day as this reader has it: her device or a parent's, signed in, or the
    household with the sign-in off."""
    if reader == "sign-in off":
        with browser(key=key) as client:
            yield client
        return
    with client_for(signed_in_household(tmp_path)) as client:
        signed_in(client, HERS if reader == "her" else THEIRS)
        yield client


def kept(
    store: ProjectStateStore,
    *seen: tuple[str, SourceChannel | None],
    applies: frozenset[str],
    assignment_id: str = ESSAY_ID,
) -> None:
    """Keep these instructions: the first alone, which applies as a first instruction does,
    and the rest beside it by a choice of which apply."""
    (first, channel), *rest = seen
    store.settle_school_instructions(
        assignment_id,
        [InstructionSeen(first, channel)],
        None,
        authored_by="parent",
        now=NOW,
        today=TODAY,
    )
    if rest:
        standing = store.school_instruction_readings([assignment_id]).readable[assignment_id]
        store.settle_school_instructions(
            assignment_id,
            [InstructionSeen(text, given) for text, given in rest],
            InstructionChoice(standing.revision, tuple(text for text, _ in seen), applies),
            authored_by="parent",
            now=NOW,
            today=TODAY,
        )


def none_applies(store: ProjectStateStore, assignment_id: str = ESSAY_ID) -> None:
    """A parent's choice that none of the kept instructions applies now."""
    standing = store.school_instruction_readings([assignment_id]).readable[assignment_id]
    store.settle_school_instructions(
        assignment_id,
        [],
        InstructionChoice(
            standing.revision, tuple(row.text for row in standing.kept), frozenset(), True
        ),
        authored_by="parent",
        now=NOW,
        today=TODAY,
    )


def waiting(
    store: ProjectStateStore, words: str, channel: SourceChannel, assignment_id: str = ESSAY_ID
) -> None:
    """A school note found in the old note field at a start, kept waiting for a parent's
    review beside the instructions already kept, as the startup rule keeps it."""
    revision = store.school_instruction_readings([assignment_id]).readable[assignment_id].revision
    with store._lock, store._writing():
        store._insert_instruction_locked(
            assignment_id,
            InstructionSeen(words, channel),
            "awaiting",
            revision + 1,
            imported_by=None,
            settled_by=None,
            now=None,
            today=None,
        )


def left_in_the_note_field(store: ProjectStateStore, words: str, mark: str | None) -> None:
    """A school note still in the old note field, with the mark it was written with."""
    row = store._connection.execute(
        "SELECT origins FROM assignments WHERE assignment_id = ?", (ESSAY_ID,)
    ).fetchone()
    origins = json.loads(row[0]) if row[0] else {}
    origins.pop("note", None)
    if mark is not None:
        origins["note"] = mark
    store._connection.execute(
        "UPDATE assignments SET note = ?, origins = ? WHERE assignment_id = ?",
        (words, json.dumps(origins) if origins else None, ESSAY_ID),
    )
    store._connection.commit()


def with_a_parents_note(store: ProjectStateStore, words: str) -> None:
    """A note a parent typed with the essay."""
    essay = store.one_assignment(ESSAY_ID)
    assert essay is not None
    store.upsert_assignments(
        [
            essay.model_copy(
                update={
                    "note": words,
                    "origins": {**essay.origins, "note": SourceChannel.PARENT_ENTRY},
                }
            )
        ]
    )


def unreadable(store: ProjectStateStore, words: str) -> None:
    """One kept instruction's row bent out of shape, so the whole set cannot be read."""
    store._connection.execute(
        "UPDATE school_instructions SET state = 'bent' WHERE assignment_id = ? AND text = ?",
        (ESSAY_ID, words),
    )
    store._connection.commit()


def the_record(store: ProjectStateStore) -> dict[str, list[tuple[object, ...]]]:
    """Every row a page about an assignment could be taken to write."""
    return {
        name: store._connection.execute(f"SELECT * FROM {name} ORDER BY rowid").fetchall()  # noqa: S608
        for name in ("assignments", "school_instructions", "student_reports", "hand_in_events")
    }


def reported(store: ProjectStateStore, status: str, assignment_id: str = ESSAY_ID) -> None:
    """Her update, saved over whatever stands, as a card showing the latest saves it."""
    events = store.student_reports(assignment_id)
    store.report_status(
        assignment_id,
        status,  # type: ignore[arg-type]
        None,
        expected_head=events[-1].report_id if events else None,
        now=NOW,
        today=TODAY,
    )
    assert store.student_reports(assignment_id)[-1].status == status


def turned_in(store: ProjectStateStore, assignment_id: str = ESSAY_ID) -> None:
    """Her word that the work is turned in, over whatever she said before."""
    reading = store.hand_in_readings([assignment_id]).readable[assignment_id]
    store.record_hand_in(
        assignment_id,
        TURNED_IN,
        None,
        None,
        expected_head=reading.head_id,
        now=NOW,
        today=TODAY,
    )
    assert store.hand_in_readings([assignment_id]).readable[assignment_id].state == TURNED_IN


# ------------------------------------------------------------- reading a page


def main_of(page: str) -> str:
    """The page's main part, where every fact and control of the page is."""
    return page.split('<main id="main">', 1)[1].split("</main>", 1)[0]


def tags(page: str) -> list[str]:
    """Every opening tag on the page, in order."""
    return re.findall(r"<[a-zA-Z][^<>]*>", page)


def focused_on_arrival(page: str) -> list[str]:
    """The opening tag of each element the page asks to take the focus as it arrives."""
    return [tag for tag in tags(page) if re.search(r"\sautofocus(?=[\s>])", tag)]


def tag_with_id(page: str, name: str) -> str:
    """The opening tag of the one element with this id; empty when none has it."""
    found = [tag for tag in tags(page) if f' id="{name}"' in tag]
    assert len(found) <= 1, (name, found)
    return found[0] if found else ""


def summary_of(page: str) -> str:
    """The summary at the top of the details, whole, or empty when there is none."""
    found = re.search(r'<p class="problem"[^>]*id="problem-summary"[^>]*>.*?</p>', page, re.S)
    return found.group(0) if found else ""


def folds(page: str) -> list[tuple[int, int]]:
    """Where each native disclosure starts and ends on the page."""
    spans: list[tuple[int, int]] = []
    open_at: list[int] = []
    for found in re.finditer(r"<details\b|</details>", page):
        if found.group(0) == "</details>":
            spans.append((open_at.pop(), found.end()))
        else:
            open_at.append(found.start())
    return spans


def folded_around(page: str, at: int) -> list[str]:
    """The opening tag of every disclosure around a place on the page."""
    return [
        page[start : page.index(">", start) + 1] for start, end in folds(page) if start < at < end
    ]


def in_a_fold(page: str, piece: str) -> bool:
    """Whether every place this piece is shown is inside some disclosure."""
    places = [found.start() for found in re.finditer(re.escape(piece), page)]
    assert places, piece
    return all(folded_around(page, at) for at in places)


def applying_lines(page: str) -> list[str]:
    """Every line the page says in the applying form."""
    return re.findall(r'<p class="from-teacher">.*?</p>', page, re.S)


def in_order(page: str, pieces: list[str]) -> None:
    """Every piece is on the page, in this order."""
    places = [page.index(piece) for piece in pieces]
    assert places == sorted(places), list(zip(pieces, places, strict=True))


def check_focus_rules(page: str) -> None:
    """One automatic focus at most, no place put ahead of the page's own order, and every
    link from the summary to a place on this same response that can take the focus."""
    assert len(focused_on_arrival(page)) <= 1, focused_on_arrival(page)
    assert re.search(r'tabindex="[1-9]', page) is None
    for fragment in re.findall(r'href="#([^"]+)"', summary_of(page)):
        target = tag_with_id(page, unescape(fragment))
        assert target, fragment
        assert 'tabindex="-1"' in target or target.startswith(("<input", "<textarea")), target


# ------------------------------------------------------------- the order (T-U6)


def crowded(store: ProjectStateStore) -> str:
    """Homework she added from her own note, with the whole crowd of the school's words: a
    40,000-character instruction and another that apply, an earlier one, one waiting for a
    parent's review, the school's Missing, and her update; the assignment's id."""
    made = homework_from_a_note(store, note="Show the working.")
    kept(
        store,
        (LONG, SourceChannel.LMS),
        (B, SourceChannel.EMAIL),
        (C, None),
        applies=frozenset({LONG, B}),
        assignment_id=made,
    )
    waiting(store, D, SourceChannel.EMAIL, assignment_id=made)
    store.record_status_reports(made, [school_missing(TODAY)])
    reported(store, "not_yet", made)
    return made


def crowded_old_note(store: ProjectStateStore) -> str:
    """The essay with the same crowd, and a school note left in the old note field."""
    kept(
        store,
        (LONG, SourceChannel.LMS),
        (B, SourceChannel.EMAIL),
        (C, None),
        applies=frozenset({LONG, B}),
    )
    waiting(store, D, SourceChannel.EMAIL)
    left_in_the_note_field(store, LEGACY, "EMAIL")
    store.record_status_reports(ESSAY_ID, [school_missing(TODAY)])
    reported(store, "not_yet")
    return ESSAY_ID


def all_done(store: ProjectStateStore) -> str:
    """The essay reported Done and turned in, with nothing of the school's kept."""
    reported(store, "done")
    turned_in(store)
    return ESSAY_ID


def quiet(store: ProjectStateStore) -> str:
    """The essay as the fixture has it: nothing of the school's, no note, no update."""
    return ESSAY_ID


STATES: dict[str, Callable[[ProjectStateStore], str]] = {
    "quiet": quiet,
    "crowded": crowded,
    "crowded, old note field": crowded_old_note,
    "all done": all_done,
}


@pytest.mark.parametrize("reader", READERS)
@pytest.mark.parametrize("state", list(STATES))
def test_the_details_say_what_the_work_is_before_they_ask_about_it(
    tmp_path: pathlib.Path, state: str, reader: str
) -> None:
    """The title and date, the jump to the update, the school's instructions, the notes,
    the way to help, her update, Turning it in, the rest of the record, and the history, in
    that order, for her, a parent, and the household with the sign-in off. Every fact is
    shown once, the details have no update fold, and looking writes nothing."""
    with reading(reader, tmp_path) as client:
        store = store_of(client)
        name = STATES[state](store)
        before = the_record(store)
        answer = client.get(f"/student/assignments/{name}", headers=PAGE_HEADERS)
        after = the_record(store)

    page = main_of(answer.text)
    parent = reader == "a parent"
    assert answer.status_code == 200
    assert after == before
    pieces = [
        "<h1>",
        '<p class="due">',
        HER_JUMP if parent else JUMP,
        HEADING,
        *([NOTES] if state == "crowded" else []),
        THEIR_HELP if parent else HER_HELP,
        STUDENT_HEADING if parent else UPDATE_HEADING,
        '<h2 class="update-heading" id="turning-it-in" tabindex="-1">Turning it in</h2>',
        '<section class="evidence" id="evidence" tabindex="-1">',
        *(["<summary>Update history<span"] if state != "quiet" else []),
    ]
    in_order(page, pieces)
    for piece in pieces:
        assert page.count(piece) == 1, piece
    assert page.count('id="instructions"') == page.count('id="update-or-turn-in"') == 1
    assert "update-fold" not in page
    if state == "quiet":
        assert NONE_APPLIES in page
        assert NOTES not in page
    if state.startswith("crowded"):
        assert page.count(str(escape(LONG))) == 1
        assert page.index(str(escape(LONG))) < page.index('id="update-or-turn-in"')
        assert page.count("<strong>The school reports this missing.</strong>") == 1
        assert page.index("The school reports this missing.") > page.index('id="evidence"')
        for words in (B, C, D):
            assert page.count(str(escape(words))) == 1, words
        assert (TWO_WAIT if state.endswith("field") else ONE_WAITS) in page
        assert in_a_fold(page, str(quoted(C)))
        assert not folded_around(page, page.index(str(quoted(D))))
    if state == "crowded":
        wrote = "She wrote" if parent else "You wrote"
        whose = "her" if parent else "your"
        in_order(
            page,
            [
                NOTES,
                f"{wrote}: <q>Show the working.</q>",
                f'<h3 class="update-heading">From {whose} homework note</h3>',
                "Geometry questions 4-8, heard from a classmate",
                THEIR_HELP if parent else HER_HELP,
            ],
        )
    if state == "crowded, old note field":
        assert page.count(str(escape(LEGACY))) == 1
        assert NOTES not in page
    if state == "all done":
        assert page.count('<span class="pill">Turned in</span>') == 1
        assert page.count(f'<span class="pill">{"Student" if parent else "Your"} update: Done') == 1


# ------------------------------------------------------------- the instructions (U-02)


def one_applies(store: ProjectStateStore) -> None:
    kept(store, (A, SourceChannel.LMS), applies=frozenset({A}))


def several_portal_first(store: ProjectStateStore) -> None:
    kept(
        store,
        (A, SourceChannel.LMS),
        (B, SourceChannel.EMAIL),
        (C, None),
        applies=frozenset({A, B, C}),
    )


def several_the_other_order(store: ProjectStateStore) -> None:
    kept(
        store,
        (C, None),
        (B, SourceChannel.EMAIL),
        (A, SourceChannel.LMS),
        applies=frozenset({A, B, C}),
    )


def same_words_portal_first(store: ProjectStateStore) -> None:
    kept(store, (A, SourceChannel.LMS), applies=frozenset({A}))
    kept(store, (A, SourceChannel.EMAIL), applies=frozenset({A}))


def same_words_email_first(store: ProjectStateStore) -> None:
    kept(store, (A, SourceChannel.EMAIL), applies=frozenset({A}))
    kept(store, (A, SourceChannel.LMS), applies=frozenset({A}))


def earlier_only(store: ProjectStateStore) -> None:
    kept(store, (A, SourceChannel.LMS), applies=frozenset({A}))
    none_applies(store)


def one_waiting(store: ProjectStateStore) -> None:
    kept(store, (A, SourceChannel.LMS), applies=frozenset({A}))
    waiting(store, C, SourceChannel.EMAIL)


def two_waiting(store: ProjectStateStore) -> None:
    one_waiting(store)
    waiting(store, D, SourceChannel.LMS)


def set_unreadable(store: ProjectStateStore) -> None:
    kept(store, (A, SourceChannel.LMS), (B, SourceChannel.EMAIL), applies=frozenset({A}))
    waiting(store, C, SourceChannel.EMAIL)
    unreadable(store, B)


def old_note(mark: str | None, rows: str) -> Callable[[ProjectStateStore], None]:
    """A school note left in the old note field with this mark, beside no kept instruction,
    beside one that applies, or beside a set that cannot be read."""

    def arrange(store: ProjectStateStore) -> None:
        if rows != "none kept":
            kept(store, (A, SourceChannel.LMS), (B, SourceChannel.EMAIL), applies=frozenset({A}))
        if rows == "unreadable":
            unreadable(store, B)
        left_in_the_note_field(store, LEGACY, mark)

    return arrange


def same_words_kept_and_left(store: ProjectStateStore) -> None:
    kept(store, (A, SourceChannel.LMS), applies=frozenset({A}))
    left_in_the_note_field(store, A, "EMAIL")


CASES: dict[str, tuple[Callable[[ProjectStateStore], None], list[str], list[str]]] = {
    # case: (arrange, shown on the details, never shown on the details)
    "none kept": (
        lambda store: None,
        [HEADING, NONE_APPLIES],
        [WAITING_HEADING, FOLD, "is waiting", "These are the school's words", UNAVAILABLE],
    ),
    "one applies": (
        one_applies,
        [HEADING, applying("school portal", A)],
        [NONE_APPLIES, WAITING_HEADING, FOLD, "From the school:"],
    ),
    "several, portal first": (
        several_portal_first,
        [applying("school", C), applying("school email", B), applying("school portal", A)],
        [NONE_APPLIES, FOLD],
    ),
    "several, the other order": (
        several_the_other_order,
        [applying("school", C), applying("school email", B), applying("school portal", A)],
        [NONE_APPLIES, FOLD],
    ),
    "same words, portal first": (
        same_words_portal_first,
        [applying("school portal", A)],
        [applying("school email", A), NONE_APPLIES],
    ),
    "same words, email first": (
        same_words_email_first,
        [applying("school email", A)],
        [applying("school portal", A), NONE_APPLIES],
    ),
    "earlier only": (
        earlier_only,
        [NONE_APPLIES, FOLD, earlier_item("school portal", A)],
        [applying("school portal", A), WAITING_HEADING, "is waiting"],
    ),
    "one waiting": (
        one_waiting,
        [
            ONE_WAITS,
            applying("school portal", A),
            WAITING_HEADING,
            waiting_item("school email", C),
        ],
        [FOLD, NONE_APPLIES],
    ),
    "two waiting": (
        two_waiting,
        [
            TWO_WAIT,
            WAITING_HEADING,
            waiting_item("school email", C),
            waiting_item("school portal", D),
        ],
        [FOLD, ONE_WAITS],
    ),
    "unreadable": (
        set_unreadable,
        [HEADING, UNAVAILABLE],
        [
            NONE_APPLIES,
            "is waiting",
            "are waiting",
            "Review school instructions",
            str(escape(A)),
            str(escape(B)),
            str(escape(C)),
            WAITING_HEADING,
        ],
    ),
    "same words kept and left in the old note field": (
        same_words_kept_and_left,
        [ONE_WAITS, applying("school portal", A), old_note_item(A, "EMAIL")],
        [NONE_APPLIES],
    ),
}
for _mark in (None, "LMS", "EMAIL"):
    CASES[f"old note field, {_mark or 'no'} mark, none kept"] = (
        old_note(_mark, "none kept"),
        [ONE_WAITS, NONE_APPLIES, WAITING_HEADING, old_note_item(LEGACY, _mark)],
        [UNAVAILABLE, FOLD, "These are the school's words"],
    )
    CASES[f"old note field, {_mark or 'no'} mark, beside one that applies"] = (
        old_note(_mark, "readable"),
        [ONE_WAITS, applying("school portal", A), old_note_item(LEGACY, _mark), FOLD],
        [NONE_APPLIES, UNAVAILABLE],
    )
    CASES[f"old note field, {_mark or 'no'} mark, beside a set that cannot be read"] = (
        old_note(_mark, "unreadable"),
        [UNREADABLE_BESIDE_A_NOTE, WAITING_HEADING, old_note_item(LEGACY, _mark)],
        [UNAVAILABLE, NONE_APPLIES, "is waiting", str(escape(A)), "Review school instructions"],
    )


@pytest.mark.parametrize("reader", READERS)
@pytest.mark.parametrize("case", list(CASES))
def test_the_instructions_are_said_whole_with_where_each_came_from(
    tmp_path: pathlib.Path, case: str, reader: str
) -> None:
    """Each state of the school's words, on the details and on her week's card. The details
    name each instruction's own channel, in the one order; say that none applies when none
    does, and that they cannot be read when they cannot, never both; list what waits for a
    parent's review in the open, apart from what applies; and fold only what was said
    before. The card says the school's words as it does. Looking writes nothing."""
    arrange, shown, never = CASES[case]
    with reading(reader, tmp_path) as client:
        store = store_of(client)
        arrange(store)
        before = the_record(store)
        answer = client.get(f"{DETAILS}?return_to=week", headers=PAGE_HEADERS)
        week = client.get(HER_PAGE, headers=PAGE_HEADERS).text
        after = the_record(store)

    page = main_of(answer.text)
    card = card_for(week, ESSAY_ID)
    assert answer.status_code == 200
    assert after == before
    in_order(page, [HEADING, *shown, 'id="update-or-turn-in"'])
    for piece in never:
        assert piece not in page, piece
    assert not (NONE_APPLIES in page and UNAVAILABLE in page)
    assert not (NONE_APPLIES in page and UNREADABLE_BESIDE_A_NOTE in page)
    assert not (UNAVAILABLE in page and UNREADABLE_BESIDE_A_NOTE in page)
    section = page[page.index(HEADING) : page.index('id="update-or-turn-in"')]
    for line in applying_lines(section):
        assert "waiting" not in line
        assert "old note field" not in line
    if WAITING_HEADING in page:
        waits = page[page.index(WAITING_HEADING) :]
        for item in re.findall(r"<li>.*?</li>", waits.split("</ul>", 1)[0], re.S):
            assert not folded_around(page, page.index(item)), item
            assert item not in "".join(applying_lines(page))
    if FOLD in page:
        for item in re.findall(r"<li>From the [^<]*, earlier: .*?</li>", page, re.S):
            assert folded_around(page, page.index(item)) == [
                '<details class="earlier-instructions">'
            ]
    to_whom = "her" if reader == "a parent" else "your"
    something_kept = case != "none kept" and not case.endswith("none kept")
    assert ("These are the school's words." in page) == something_kept
    if something_kept:
        assert f"nor {to_whom} own updates." in page
    set_unread = case == "unreadable" or case.endswith("cannot be read")
    offers_review = reader == "a parent" and something_kept and not set_unread
    assert ("Review school instructions</a>" in page) == offers_review
    if case == "one applies":
        assert f"From the school: {quoted(A)}" in card
    if case.startswith("several"):
        assert card.count("From the school: ") == 3
    if case in ("one waiting", "two waiting"):
        assert CARD_FOLD in card
        assert in_a_fold(card, str(quoted(C)))
    if set_unread:
        assert UNAVAILABLE in card
        assert UNREADABLE_BESIDE_A_NOTE not in card


def test_each_line_keeps_the_channel_of_its_own_row() -> None:
    """The view is made from rows whose channels run against the order they are listed in:
    each line keeps its own row's words and channel, whatever its place. A school note left
    in the old note field keeps its own mark when it names a school channel, and nothing
    else is read as one."""

    def row(
        sequence: int, words: str, channel: SourceChannel | None, state: str
    ) -> SchoolInstruction:
        return SchoolInstruction(
            sequence=sequence,
            assignment_id=ESSAY_ID,
            text=words,
            channel=channel,
            card=None,
            card_day=None,
            first_seen_at=None,
            first_seen_on=None,
            imported_by=None,
            state=state,  # type: ignore[arg-type]
            settled_by=None,
            settled_at=None,
            settled_on=None,
            revision=sequence,
        )

    standing = InstructionsStanding(
        current=(
            row(3, C, SourceChannel.EMAIL, "current"),
            row(1, B, None, "current"),
            row(2, A, SourceChannel.LMS, "current"),
        ),
        history=(row(5, D, SourceChannel.LMS, "history"), row(4, A + " Again.", None, "history")),
        awaiting=(
            row(7, "Waits.", None, "awaiting"),
            row(6, "Also waits.", SourceChannel.EMAIL, "awaiting"),
        ),
        revision=7,
    )
    marks = {
        "LMS": SourceChannel.LMS,
        "EMAIL": SourceChannel.EMAIL,
        "PARENT_ENTRY": None,
        "STUDENT_REPORT": None,
        "none": None,
    }
    for mark, channel in marks.items():
        origins = {} if mark == "none" else {"note": SourceChannel(mark)}
        item = ESSAY.model_copy(update={"note": LEGACY, "origins": origins})
        view = school_words(item, standing, False)
        assert [(line.text, line.channel) for line in view.current] == [
            (C, SourceChannel.EMAIL),
            (B, None),
            (A, SourceChannel.LMS),
        ]
        assert [(line.text, line.channel) for line in view.earlier] == [
            (D, SourceChannel.LMS),
            (A + " Again.", None),
        ]
        assert [(line.text, line.channel) for line in view.awaiting] == [
            ("Waits.", None),
            ("Also waits.", SourceChannel.EMAIL),
        ]
        assert view.note_channel == channel, mark
    bare = school_words(
        ESSAY.model_copy(update={"origins": {"note": SourceChannel.LMS}}), None, False
    )
    assert bare.note is None
    assert bare.note_channel is None
    cannot = school_words(ESSAY, standing, True)
    assert (cannot.current, cannot.earlier, cannot.awaiting) == ([], [], [])


# ------------------------------------------------------------- the jump and the way back


@pytest.mark.parametrize("reader", READERS)
def test_the_jump_lands_on_the_update_heading_and_change_and_keep_land_there_too(
    tmp_path: pathlib.Path, reader: str
) -> None:
    """The jump's address and the heading's id agree, once each, on a heading that can take
    the focus and is not in the Tab order. Change and Keep it as it is land on that heading
    too. A parent's page has no form, and keeps the jump and the heading."""
    with reading(reader, tmp_path) as client:
        store = store_of(client)
        kept(store, (LONG, SourceChannel.LMS), applies=frozenset({LONG}))
        reported(store, "done")
        page = client.get(
            f"{DETAILS}?return_to=week&week={FIXTURE_WEEK}", headers=PAGE_HEADERS
        ).text
        editor = client.get(
            f"{DETAILS}?return_to=week&week={FIXTURE_WEEK}&change=1", headers=PAGE_HEADERS
        ).text

    jump = re.search(r'<p class="jump"><a href="#([^"]+)">', page)
    assert jump is not None
    assert jump.group(1) == "update-or-turn-in"
    assert page.count('id="update-or-turn-in"') == 1
    assert tag_with_id(page, "update-or-turn-in") == (
        '<h2 class="update-heading" id="update-or-turn-in" tabindex="-1">'
    )
    assert tag_with_id(page, "instructions") == (
        '<h2 class="update-heading" id="instructions" tabindex="-1">'
    )
    assert (
        page.index(jump.group(0))
        < page.index(str(escape(LONG)))
        < page.index('id="update-or-turn-in"')
    )
    if reader == "a parent":
        assert "<form" not in main_of(page)
        assert "<form" not in main_of(editor)
        return
    change = f"{DETAILS}#update-or-turn-in"
    assert form_fields(page, change) == {
        "return_to": "week",
        "week": FIXTURE_WEEK,
        "change": "1",
    }
    keep = re.search(r'<a class="cancel" href="([^"]+)"', editor)
    assert keep is not None
    assert unescape(keep.group(1)) == (
        f"{DETAILS}?return_to=week&week={FIXTURE_WEEK}#update-or-turn-in"
    )


ORIGINS: dict[str, dict[str, str]] = {
    "her week": {"return_to": "week"},
    "another week": {"return_to": "week", "week": "2026-08-24"},
    "today": {"return_to": "today"},
    "to turn in": {"return_to": "to_turn_in"},
    "family": {"return_to": "family"},
    "family, a plan": {"return_to": "family", "plan_id": ""},
}


def way_back(page: str) -> str:
    """The way back at the top of the details, as the page writes it."""
    found = re.search(r'<p class="return"><a href="[^"]+">[^<]+</a>', page)
    assert found is not None
    return found.group(0)


def changed(client: TestClient, page: str) -> str:
    """The details as Change opens them, from the Change form the page wrote."""
    fields = form_fields(page, f"{DETAILS}#update-or-turn-in")
    return client.get(DETAILS, params=fields, headers=PAGE_HEADERS).text


@pytest.mark.parametrize("origin", list(ORIGINS))
def test_every_way_back_holds_through_change_keep_save_undo_and_a_conflict(origin: str) -> None:
    """From each place the details can be reached from, the way back at the top is the same
    on arrival, after a save, after an Undo, after Change, after Keep it as it is, and on a
    conflict; a save and an Undo land where they always have."""
    with browser(key=True) as client:
        carried = dict(ORIGINS[origin])
        if "plan_id" in carried:
            walkthrough(client)
            carried["plan_id"] = planned(client).draft_id
        arrived = client.get(DETAILS, params=carried, headers=PAGE_HEADERS).text
        saved = client.post(
            REPORT,
            data={**form_fields(arrived, REPORT), "status": "done", "note": ""},
            headers=PAGE_HEADERS,
        )
        after_save = client.get(saved.headers["location"], headers=PAGE_HEADERS).text
        undone = client.post(UNDO, data=form_fields(after_save, UNDO), headers=PAGE_HEADERS)
        undo_id = store_of(client).student_reports(ESSAY_ID)[-1].report_id
        after_undo = client.get(undone.headers["location"], headers=PAGE_HEADERS).text
        again = client.post(
            REPORT,
            data={**form_fields(after_undo, REPORT), "status": "not_yet", "note": ""},
            headers=PAGE_HEADERS,
        )
        standing = client.get(again.headers["location"], headers=PAGE_HEADERS).text
        editor = changed(client, standing)
        keep = re.search(r'<a class="cancel" href="([^"]+)"', editor)
        assert keep is not None
        kept_as_is = client.get(unescape(keep.group(1)), headers=PAGE_HEADERS).text
        reported(store_of(client), "done")
        conflict = client.post(
            REPORT,
            data={**form_fields(editor, REPORT), "status": "not_yet", "note": "Mine."},
            headers=PAGE_HEADERS,
        )

    back = "&".join(f"{name}={quote(value, safe='')}" for name, value in carried.items())
    assert saved.status_code == undone.status_code == again.status_code == 303
    landing = landing_in(saved.headers["location"])
    assert saved.headers["location"] == (
        f"{DETAILS}?said=saved&{back}&landing={landing}#update-result-{ESSAY_ID}"
    )
    assert undone.headers["location"] == (
        f"{DETAILS}?said=undone&undo_event={undo_id}&{back}#update-result-{ESSAY_ID}"
    )
    assert unescape(keep.group(1)) == f"{DETAILS}?{back}#update-or-turn-in"
    assert conflict.status_code == 409
    first = way_back(arrived)
    for page in (after_save, after_undo, standing, editor, kept_as_is, conflict.text):
        assert way_back(page) == first


# ------------------------------------------------------------- focus and alerts (U-04)


def details_as_her(client: TestClient) -> str:
    return client.get(f"{DETAILS}?return_to=week", headers=PAGE_HEADERS).text


def save_from(
    client: TestClient, page: str, status: str | None, note: str = "", **over: str
) -> Answer:
    """Send the update form a page wrote, with her choice."""
    fields = form_fields(page, REPORT)
    chosen = {} if status is None else {"status": status}
    return client.post(
        REPORT, data={**fields, **chosen, "note": note, **over}, headers=PAGE_HEADERS
    )


def conflict(client: TestClient) -> Answer:
    page = details_as_her(client)
    assert save_from(client, page, "not_yet", "From the other tab.").status_code == 303
    return save_from(client, page, "done", "From this tab.")


def conflict_on_done(client: TestClient) -> Answer:
    reported(store_of(client), "done")
    editor = client.get(f"{DETAILS}?return_to=week&change=1", headers=PAGE_HEADERS).text
    reported(store_of(client), "not_yet")
    return save_from(client, editor, "done", "Again.")


def stale_undo(client: TestClient) -> Answer:
    reported(store_of(client), "done")
    page = details_as_her(client)
    reported(store_of(client), "not_yet")
    return client.post(UNDO, data=form_fields(page, UNDO), headers=PAGE_HEADERS)


def already_undone(client: TestClient) -> Answer:
    reported(store_of(client), "done")
    page = details_as_her(client)
    assert client.post(UNDO, data=form_fields(page, UNDO), headers=PAGE_HEADERS).status_code == 303
    return client.post(UNDO, data=form_fields(page, UNDO), headers=PAGE_HEADERS)


def malformed(client: TestClient) -> Answer:
    fields = form_fields(details_as_her(client), REPORT)
    return client.post(
        REPORT, data={**fields, "status": "done", "note": ["one", "two"]}, headers=PAGE_HEADERS
    )


def bad_return(client: TestClient) -> Answer:
    return save_from(
        client, details_as_her(client), "done", "kept", return_to="https://example.org/"
    )


def not_this_cards(client: TestClient) -> Answer:
    reported(store_of(client), "done", QUIZ_ID)
    theirs = store_of(client).student_reports(QUIZ_ID)[-1].report_id
    return save_from(client, details_as_her(client), "done", "kept", expected_report_id=theirs)


def refused_by_the_file(client: TestClient) -> Answer:
    page = details_as_her(client)
    store = store_of(client)
    store._connection.execute(
        "CREATE TRIGGER refuse_reports BEFORE INSERT ON student_reports "
        "BEGIN SELECT RAISE(ABORT, 'refused'); END"
    )
    store._connection.commit()
    return save_from(client, page, "not_yet", "kept <words>")


def no_choice(client: TestClient) -> Answer:
    return save_from(client, details_as_her(client), None, "kept")


def note_too_long(client: TestClient) -> Answer:
    return save_from(client, details_as_her(client), "done", "x" * 501)


def hand_in_opened(client: TestClient) -> str:
    return client.get(f"{DETAILS}?return_to=week&hand_in=change", headers=PAGE_HEADERS).text


def hand_in_no_state(client: TestClient) -> Answer:
    fields = whole_form(hand_in_opened(client), HAND_IN)
    fields.pop("state", None)
    return client.post(HAND_IN, data=fields, headers=PAGE_HEADERS)


def hand_in_conflict(client: TestClient) -> Answer:
    opened = hand_in_opened(client)
    turned_in(store_of(client))
    return client.post(
        HAND_IN, data={**whole_form(opened, HAND_IN), "state": NEEDS_HAND_IN}, headers=PAGE_HEADERS
    )


def hand_in_refused_by_the_file(client: TestClient) -> Answer:
    opened = hand_in_opened(client)
    store = store_of(client)
    store._connection.execute(
        "CREATE TRIGGER refuse_hand_in BEFORE INSERT ON hand_in_events "
        "BEGIN SELECT RAISE(ABORT, 'refused'); END"
    )
    store._connection.commit()
    return client.post(
        HAND_IN, data={**whole_form(opened, HAND_IN), "state": TURNED_IN}, headers=PAGE_HEADERS
    )


def saved(client: TestClient) -> Answer:
    answer = save_from(client, details_as_her(client), "done")
    assert answer.status_code == 303
    return client.get(answer.headers["location"], headers=PAGE_HEADERS)


PARENTS_SAVE = {
    "status": "done",
    "note": "",
    "expected_report_id": "",
    "report_view": "detail",
    "return_to": "family",
    "week": "",
    "plan_id": "",
}


def a_parents_save(client: TestClient) -> Answer:
    return client.post(REPORT, data=PARENTS_SAVE, headers=PAGE_HEADERS)


def a_parents_hand_in(client: TestClient) -> Answer:
    return client.post(
        HAND_IN,
        data={
            "state": TURNED_IN,
            "next_action": "",
            "note": "",
            "expected_hand_in_id": "",
            "return_to": "family",
            "week": "",
            "plan_id": "",
        },
        headers=PAGE_HEADERS,
    )


# case: (make the response, reader, status, what takes the focus, the problem's words)
SUMMARY = '<p class="problem" role="alert" id="problem-summary" tabindex="-1" autofocus>'
DETAILS_CASES: dict[str, tuple[Callable[[TestClient], Answer], str, int, str, str]] = {
    "conflict": (conflict, "her", 409, SUMMARY, "An update was saved on another device."),
    "conflict on a Done card": (
        conflict_on_done,
        "her",
        409,
        SUMMARY,
        "An update was saved on another device.",
    ),
    "stale undo": (stale_undo, "her", 409, SUMMARY, "Your update has changed"),
    "already undone": (already_undone, "her", 409, SUMMARY, "That update was already undone."),
    "malformed form": (malformed, "her", 422, SUMMARY, "That form carried a field twice"),
    "bad return": (bad_return, "her", 422, SUMMARY, "The form named a page to go back to"),
    "not this card's": (not_this_cards, "her", 422, SUMMARY, "That form names an update"),
    "failed write": (
        refused_by_the_file,
        "her",
        500,
        SUMMARY,
        "Your update could not be saved. Your words are still here. Try again.",
    ),
    "no choice": (
        no_choice,
        "her",
        422,
        '<input type="radio" name="status" value="done" autofocus>',
        "Choose Done or Not yet.",
    ),
    "note too long": (note_too_long, "her", 422, "<textarea", "Keep your note to 500"),
    "hand-in field": (
        hand_in_no_state,
        "her",
        422,
        f'<input id="hand-in-state-{ESSAY_ID}" type="radio" name="state" '
        'value="needs_hand_in" autofocus>',
        "Choose one: Still to turn in",
    ),
    "hand-in conflict": (hand_in_conflict, "her", 409, SUMMARY, "These details changed"),
    "hand-in failed write": (
        hand_in_refused_by_the_file,
        "her",
        500,
        SUMMARY,
        "Your hand-in update could not be saved.",
    ),
    "saved": (saved, "her", 200, "", ""),
}


@pytest.mark.parametrize("case", list(DETAILS_CASES))
def test_on_the_details_a_refusal_takes_the_focus_at_its_summary_or_its_field(
    tmp_path: pathlib.Path, case: str
) -> None:
    """Every refusal on the details is said once as an alert, at the summary. A field's
    refusal gives the field the focus; any other gives the summary the focus. The update's
    and the hand-in's own problem lines are plain text the summary links to, and each link
    lands on something on the same page that can take the focus. A save's page asks for no
    focus: its address lands on the result."""
    make, reader, status, focus, words = DETAILS_CASES[case]
    with reading(reader, tmp_path) as client:
        answer = make(client)

    page = main_of(answer.text)
    assert answer.status_code == status, answer.text[:300]
    check_focus_rules(answer.text)
    asked = focused_on_arrival(answer.text)
    if not focus:
        assert asked == []
        assert 'role="alert"' not in page
        assert tag_with_id(page, f"update-result-{ESSAY_ID}").endswith('tabindex="-1">')
        return
    assert words in summary_of(page)
    assert page.count('role="alert"') == 1
    assert len(asked) == 1
    assert asked[0].startswith(focus), asked
    if focus != SUMMARY:
        assert " autofocus" not in summary_of(page)
    update_problem = tag_with_id(page, f"update-problem-{ESSAY_ID}")
    if update_problem:
        assert update_problem == f'<p class="problem" id="update-problem-{ESSAY_ID}" tabindex="-1">'
        assert f'<a href="#update-problem-{ESSAY_ID}">Go to the update.</a>' in summary_of(page)
    hand_in_problem = tag_with_id(page, f"hand-in-problem-{ESSAY_ID}")
    if hand_in_problem:
        assert (
            hand_in_problem == f'<p class="problem" id="hand-in-problem-{ESSAY_ID}" tabindex="-1">'
        )


def test_a_failed_write_whose_page_cannot_be_read_back_focuses_its_own_explanation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The plain page for a save the file refused, when the details cannot be read back
    either, keeps its one explanation, which takes the focus."""
    with browser() as client:
        page = details_as_her(client)
        store = store_of(client)
        store._connection.execute(
            "CREATE TRIGGER refuse_reports BEFORE INSERT ON student_reports "
            "BEGIN SELECT RAISE(ABORT, 'refused'); END"
        )
        store._connection.commit()

        def cannot(*_: object, **__: object) -> None:
            msg = "the record could not be read"
            raise RuntimeError(msg)

        monkeypatch.setattr(student_routes, "detail_page", cannot)
        answer = save_from(client, page, "not_yet", "kept")

    assert answer.status_code == 500
    check_focus_rules(answer.text)
    assert focused_on_arrival(answer.text) == [SUMMARY]
    assert main_of(answer.text).count('role="alert"') == 1


def week_conflict(client: TestClient) -> Answer:
    card = week_card(client)
    reported(store_of(client), "not_yet")
    return week_save(client, card, "done", "Mine.")


def week_conflict_on_done(client: TestClient) -> Answer:
    reported(store_of(client), "done")
    card = week_card(client)
    reported(store_of(client), "not_yet")
    reported(store_of(client), "done")
    return week_save(client, card, "done", "Mine.")


def week_conflict_apart(client: TestClient) -> Answer:
    card = week_card(client)
    dated(store_of(client), None, date(2026, 8, 28))
    reported(store_of(client), "not_yet")
    return week_save(client, card, "done", "Mine.")


def week_stale_undo(client: TestClient) -> Answer:
    reported(store_of(client), "done")
    card = card_for(client.get(HER_PAGE, headers=PAGE_HEADERS).text, ESSAY_ID)
    reported(store_of(client), "not_yet")
    return client.post(UNDO, data=form_fields(card, UNDO), headers=PAGE_HEADERS)


def week_saved(client: TestClient) -> Answer:
    answer = week_save(client, week_card(client), "done")
    assert answer.status_code == 303
    return client.get(answer.headers["location"], headers=PAGE_HEADERS)


def week_parents_save(client: TestClient) -> Answer:
    return client.post(
        REPORT,
        data={"status": "done", "note": "", "expected_report_id": "", "week": FIXTURE_WEEK},
        headers=PAGE_HEADERS,
    )


def gone_parents_save(client: TestClient) -> Answer:
    gone(client)
    return a_parents_save(client)


def week_undo_on_gone_work(client: TestClient) -> Answer:
    reported(store_of(client), "done")
    card = card_for(client.get(HER_PAGE, headers=PAGE_HEADERS).text, ESSAY_ID)
    store = store_of(client)
    store._connection.execute("DELETE FROM assignments WHERE assignment_id = ?", (ESSAY_ID,))
    store._connection.commit()
    return client.post(UNDO, data=form_fields(card, UNDO), headers=PAGE_HEADERS)


WEEK_PROBLEM = f'<p class="problem" role="alert" id="update-problem-{ESSAY_ID}" tabindex="-1"'
WEEK_TOP = '<p class="problem week-problem" role="alert" tabindex="-1" autofocus>'
# case: (make the response, reader, status, alerts, what takes the focus)
WEEK_CASES: dict[str, tuple[Callable[[TestClient], Answer], str, int, int, str]] = {
    "conflict": (week_conflict, "her", 409, 1, WEEK_PROBLEM + " autofocus>"),
    "conflict on a Done card": (week_conflict_on_done, "her", 409, 1, WEEK_PROBLEM + " autofocus>"),
    "conflict on the card shown apart": (
        week_conflict_apart,
        "her",
        409,
        1,
        WEEK_PROBLEM + " autofocus>",
    ),
    "stale undo": (week_stale_undo, "her", 409, 1, WEEK_PROBLEM + " autofocus>"),
    "malformed form": (week_malformed, "her", 422, 1, WEEK_PROBLEM + " autofocus>"),
    "failed write": (week_refused_by_the_file, "her", 500, 1, WEEK_PROBLEM + " autofocus>"),
    "no choice": (
        week_no_choice,
        "her",
        422,
        1,
        '<input type="radio" name="status" value="done" autofocus>',
    ),
    "saved": (week_saved, "her", 200, 0, ""),
    "a parent's save": (week_parents_save, "a parent", 403, 1, WEEK_TOP),
    "a parent's save from the details": (a_parents_save, "a parent", 403, 1, WEEK_TOP),
    "a parent's hand-in from the details": (a_parents_hand_in, "a parent", 403, 1, WEEK_TOP),
    "a parent's save on work gone": (gone_parents_save, "a parent", 403, 1, WEEK_TOP),
    "an Undo on work gone from the record": (week_undo_on_gone_work, "her", 404, 1, WEEK_TOP),
}


@pytest.mark.parametrize("case", list(WEEK_CASES))
def test_on_her_week_a_refusal_is_said_once_as_an_alert_where_it_takes_the_focus(
    tmp_path: pathlib.Path, case: str
) -> None:
    """The same component on her week: the card's problem line is the one alert and takes
    the focus, unless a field does, and the top line repeats it with no alert. A refusal no
    card holds is the top line's alone, which takes the focus."""
    make, reader, status, alerts, focus = WEEK_CASES[case]
    with reading(reader, tmp_path) as client:
        answer = make(client)

    page = main_of(answer.text)
    assert answer.status_code == status, answer.text[:300]
    assert len(focused_on_arrival(answer.text)) <= 1
    assert re.search(r'tabindex="[1-9]', page) is None
    assert page.count('role="alert"') == alerts
    asked = focused_on_arrival(answer.text)
    if focus:
        assert len(asked) == 1
        assert asked[0].startswith(focus)
    else:
        assert asked == []
    if focus.startswith((WEEK_PROBLEM, "<input")):
        assert WEEK_PROBLEM in page
        assert '<p class="problem week-problem">' in page
    if case == "conflict on a Done card":
        assert '<details class="steps reported-done" open>' in page
    if case == "conflict on the card shown apart":
        assert WEEK_PROBLEM in page[page.index('<section class="panel apart">') :]
    if case == "an Undo on work gone from the record":
        assert f"{WEEK_TOP}That assignment is not on record, so nothing was changed.</p>" in page
    if reader == "a parent":
        assert "<h1>Student week</h1>" in page
        assert f"{WEEK_TOP}Sign in as the student to update.</p>" in page
        assert "This assignment is not on record now." not in page


# ------------------------------------------------------------- the page for work gone


GONE_TAG = '<p class="problem" role="alert" id="problem-summary" tabindex="-1">'


@pytest.mark.parametrize("address", ["", "?said=saved", "?change=1", "?hand_in=change"])
def test_a_look_at_work_not_on_record_asks_for_no_focus(address: str) -> None:
    """Asked for by a link, whatever its address carries, the page for work not on record
    says so with the one explanation, which can take the focus and asks for none."""
    with browser() as client:
        answer = client.get(f"/student/assignments/{NOWHERE}{address}", headers=PAGE_HEADERS)

    assert answer.status_code == 404
    page = main_of(answer.text)
    assert f"{GONE_TAG}This assignment is not on record now.</p>" in page
    assert focused_on_arrival(answer.text) == []
    assert answer.text.count('id="problem-summary"') == 1
    assert page.count('role="alert"') == 1
    assert re.search(r'tabindex="[1-9]', page) is None


def gone(client: TestClient) -> None:
    store = store_of(client)
    store._connection.execute("DELETE FROM assignments WHERE assignment_id = ?", (ESSAY_ID,))
    store._connection.commit()


def gone_details_save(client: TestClient) -> Answer:
    page = details_as_her(client)
    gone(client)
    return save_from(client, page, "done", "Typed before it went.")


def gone_week_save(client: TestClient) -> Answer:
    card = week_card(client)
    gone(client)
    return week_save(client, card, "done", "Typed before it went.")


def gone_details_undo(client: TestClient) -> Answer:
    reported(store_of(client), "done")
    page = details_as_her(client)
    gone(client)
    return client.post(UNDO, data=form_fields(page, UNDO), headers=PAGE_HEADERS)


def gone_hand_in(client: TestClient) -> Answer:
    opened = hand_in_opened(client)
    gone(client)
    return client.post(
        HAND_IN, data={**whole_form(opened, HAND_IN), "state": TURNED_IN}, headers=PAGE_HEADERS
    )


def gone_hand_in_undo(client: TestClient) -> Answer:
    turned_in(store_of(client))
    page = details_as_her(client)
    gone(client)
    return client.post(UNDO_HAND_IN, data=form_fields(page, UNDO_HAND_IN), headers=PAGE_HEADERS)


GONE_PRESSES: dict[str, tuple[Callable[[TestClient], Answer], str]] = {
    "a save from the details": (gone_details_save, "her"),
    "a save from her week": (gone_week_save, "her"),
    "an Undo from the details": (gone_details_undo, "her"),
    "a hand-in save": (gone_hand_in, "her"),
    "a hand-in Undo": (gone_hand_in_undo, "her"),
}


@pytest.mark.parametrize("case", list(GONE_PRESSES))
def test_after_a_press_on_work_not_on_record_its_explanation_takes_the_focus(
    tmp_path: pathlib.Path, case: str
) -> None:
    """A form pressed for work taken off the record meanwhile gets the small page, 404,
    whose one explanation takes the focus: the one thing on the page that matters."""
    make, reader = GONE_PRESSES[case]
    with reading(reader, tmp_path) as client:
        answer = make(client)

    assert answer.status_code == 404
    page = main_of(answer.text)
    assert focused_on_arrival(answer.text) == [GONE_TAG[:-1] + " autofocus>"]
    assert answer.text.count('id="problem-summary"') == 1
    assert page.count('role="alert"') == 1
    assert "This assignment is not on record now." in page
    assert re.search(r'tabindex="[1-9]', page) is None


# ------------------------------------------------------------- Read instructions on her week


def dated(store: ProjectStateStore, assigned: date | None, due: date | None) -> None:
    """The essay's days on the record, written in place, whatever note the row holds, with no
    claim about its date left to disagree."""
    store._connection.execute(
        "UPDATE assignments SET assigned_on = ?, due_date = ? WHERE assignment_id = ?",
        (
            None if assigned is None else assigned.isoformat(),
            None if due is None else due.isoformat(),
            ESSAY_ID,
        ),
    )
    store._connection.execute("DELETE FROM date_claims WHERE assignment_id = ?", (ESSAY_ID,))
    store._connection.commit()


def waiting_alone(store: ProjectStateStore) -> None:
    earlier_only(store)
    waiting(store, C, SourceChannel.EMAIL)


LINK_STATES: dict[str, tuple[Callable[[ProjectStateStore], None], bool]] = {
    "applies": (one_applies, True),
    "waits": (waiting_alone, True),
    "old note field": (old_note("EMAIL", "none kept"), True),
    "old note field beside a set that cannot be read": (old_note(None, "unreadable"), True),
    "earlier only": (earlier_only, False),
    "cannot be read": (set_unreadable, False),
    "none kept": (lambda store: None, False),
}


def read_instructions(week: str) -> str:
    return (
        f'<a class="assignment-link" href="/student/assignments/{ESSAY_ID}?return_to=week'
        f'&amp;week={week}#instructions" aria-label="Read instructions: {ESSAY_TITLE}, '
        'World History">Read instructions</a>'
    )


def read_instructions_in_a_row(week: str) -> str:
    return read_instructions(week).replace(
        '<a class="assignment-link"', '<a class="details-link assignment-link"'
    )


@pytest.mark.parametrize("reader", READERS)
@pytest.mark.parametrize("state", list(LINK_STATES))
def test_read_instructions_is_on_every_card_and_row_with_instructions_to_read(
    tmp_path: pathlib.Path, state: str, reader: str
) -> None:
    """On her card, the card shown apart, a card she reports done, and both lists of work
    due later, a link to the instructions is beside Details when an instruction applies or
    waits for review, and nowhere else. It carries the week shown, is outside the update and
    every fold but Reported done, and lands on the instructions' heading."""
    arrange, shown = LINK_STATES[state]
    with reading(reader, tmp_path) as client:
        store = store_of(client)
        arrange(store)
        places = {"a card": card_for(client.get(HER_PAGE, headers=PAGE_HEADERS).text, ESSAY_ID)}
        apart = client.get(
            HER_PAGE, params={"week": "2026-08-24", "show": ESSAY_ID}, headers=PAGE_HEADERS
        ).text
        places["the card shown apart"] = card_for(
            apart[apart.index('<section class="panel apart">') :], ESSAY_ID
        )
        dated(store, date(2026, 8, 18), date(2026, 9, 1))
        later = client.get(HER_PAGE, headers=PAGE_HEADERS).text
        places["a row due later"] = card_for(
            later.split("<h2>Assigned this week, due later</h2>", 1)[1], ESSAY_ID
        )
        reported(store, "done")
        later_done = client.get(HER_PAGE, headers=PAGE_HEADERS).text
        places["a row due later she reports done"] = card_for(
            later_done.split("<h2>Assigned this week, due later</h2>", 1)[1], ESSAY_ID
        )
        dated(store, None, date(2026, 8, 21))
        done = client.get(HER_PAGE, headers=PAGE_HEADERS).text
        places["a card she reports done"] = card_for(done, ESSAY_ID)
        landed = client.get(f"{DETAILS}?return_to=week&week=2026-08-24", headers=PAGE_HEADERS).text

    for place, card in places.items():
        week = "2026-08-24" if place == "the card shown apart" else FIXTURE_WEEK
        link = read_instructions_in_a_row(week) if "row" in place else read_instructions(week)
        assert (link in card) == shown, place
        assert card.count(">Read instructions</a>") == int(shown), place
        if not shown:
            continue
        assert card.index(link) > card.index(">Details</a>"), place
        assert card.index(link) < card.index('<div class="update">'), place
    for page, place in (
        (done, "a card she reports done"),
        (later_done, "a row due later she reports done"),
    ):
        if shown:
            around = folded_around(page, page.index(">Read instructions</a>"))
            assert around == ['<details class="steps reported-done" open>'] or around == [
                '<details class="steps reported-done">'
            ], place
    assert HEADING in landed
    assert (
        f'href="/student/due-this-week?week=2026-08-24&amp;show={ESSAY_ID}#title-{ESSAY_ID}"'
        in landed
    )


# ------------------------------------------------------------- help, and the voice


@pytest.mark.parametrize("reader", READERS)
def test_help_is_one_plain_link_to_the_help_that_is_there(
    tmp_path: pathlib.Path, reader: str
) -> None:
    """Her details, and the household's with the sign-in off, link to Ask for help on her
    week; a parent's link to the help she asked for on the family page. Each place is there
    for whoever follows the link, the link carries nothing of the assignment, and the
    details have no form to ask with."""
    with reading(reader, tmp_path) as client:
        details = client.get(DETAILS, headers=PAGE_HEADERS).text
        week = client.get(HER_PAGE, headers=PAGE_HEADERS).text
        family = client.get("/parent", headers=PAGE_HEADERS) if reader != "her" else None

    page = main_of(details)
    if reader == "a parent":
        assert THEIR_HELP in page
        assert "Ask a parent for help" not in page
        assert "Help on My week" not in page
        assert family is not None
        assert '<section id="help-she-asked-for"' in family.text
    else:
        assert HER_HELP in page
        assert NOT_ATTACHED in unescape(page)
        assert "Help she asked for" not in page
    assert 'id="ask-for-help"' in week
    help_link = re.search(
        r'<p class="support-links"><a href="([^"]+)">'
        r"(Ask a parent for help|Help she asked for)</a>",
        page,
    )
    assert help_link is not None
    assert urlsplit(unescape(help_link.group(1))).query == ""
    assert 'action="/student/actions/ask-for-help"' not in page


@pytest.mark.parametrize("reader", ["a parent", "her"])
def test_the_new_parts_speak_to_whoever_reads(tmp_path: pathlib.Path, reader: str) -> None:
    """A parent reading her details is told about her: the jump, the heading, the help
    link, her note and her homework note; nothing in the parts before her update speaks to a
    parent in the second person. She reads the same parts as hers."""
    with reading(reader, tmp_path) as client:
        store = store_of(client)
        made = homework_from_a_note(store, note="Show the working.")
        kept(store, (A, SourceChannel.LMS), applies=frozenset({A}), assignment_id=made)
        page = main_of(client.get(f"/student/assignments/{made}", headers=PAGE_HEADERS).text)

    opening = page[page.index('<p class="jump">') : page.index('id="update-or-turn-in"')]
    if reader == "a parent":
        for words in (
            "Her update and hand-in status",
            "Help she asked for",
            "From her homework note",
            "She wrote: <q>Show the working.</q>",
            "nor her own updates",
        ):
            assert words in page, words
        assert STUDENT_HEADING in page
        assert re.search(r"\b(you|your)\b", opening, re.IGNORECASE) is None
    else:
        for words in (
            "Your update and hand-in status",
            "Ask a parent for help",
            "From your homework note",
            "You wrote: <q>Show the working.</q>",
            "nor your own updates",
        ):
            assert words in page, words
        assert UPDATE_HEADING in page


# ------------------------------------------------------------- what a look cannot read


@pytest.mark.parametrize("count", [1, 2])
def test_a_look_that_cannot_read_a_part_says_so_and_never_that_nothing_was_changed(
    monkeypatch: pytest.MonkeyPatch, count: int
) -> None:
    """Homework notes or a date history that cannot be read are said to be unavailable on
    an ordinary look, which changes nothing and so says nothing about changes. The words
    for a save or an Undo the file refused stay as they are."""
    with browser() as client:
        store = store_of(client)

        def notes(assignment_id: str) -> CaptureReadings:
            return CaptureReadings(notes=[], unreadable=[f"note-{n}" for n in range(count)])

        def history(assignment_id: str) -> list[object]:
            msg = "a claim held as nothing the store writes"
            raise UnreadableClaim(msg)

        monkeypatch.setattr(store, "captures_of_assignment", notes)
        monkeypatch.setattr(store, "claim_history", history)
        answer = client.get(DETAILS, headers=PAGE_HEADERS)

    page = main_of(answer.text)
    assert answer.status_code == 200
    line = (
        "1 homework note of this assignment cannot be read right now, so it is not shown."
        if count == 1
        else "2 homework notes of this assignment cannot be read right now, so they are not shown."
    )
    assert f'<p class="problem">{line}</p>' in page
    assert (
        '<p class="problem">Date history cannot be read right now. The current record is shown '
        "above.</p>"
    ) in page
    assert "Nothing was changed" not in page
    in_order(page, [NOTES, line, 'id="update-or-turn-in"', 'id="evidence"', "Date history"])
    assert student_routes.NOT_SAVED == (
        "Your update could not be saved. Your words are still here. Try again."
    )
    assert student_routes.NOT_UNDONE == (
        "Your update could not be undone, and nothing was changed. Try again."
    )


def test_a_hand_in_record_that_cannot_be_read_is_said_as_it_is(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The hand-in line says which record can't be read, that it is left out, and that
    nothing can be saved here until it can be; the page is only read, so it says nothing of
    a change."""
    with browser() as client:
        store = store_of(client)
        monkeypatch.setattr(
            store, "hand_in_readings", lambda names: HandInReadings({}, frozenset(names))
        )
        answer = client.get(DETAILS, headers=PAGE_HEADERS)

    assert answer.status_code == 200
    assert (
        '<p class="problem">Your hand-in record for this assignment cannot be read right now, '
        "so it is not shown. Nothing can be saved here until it can be read.</p>"
    ) in main_of(answer.text)
    assert "Nothing was changed" not in main_of(answer.text)


def rules_for(css: str, selector: str) -> list[str]:
    """The declarations of every rule whose selector list holds ``selector`` itself."""
    plain = re.sub(r"/\*.*?\*/", "", css, flags=re.S)
    return [
        inside
        for head, inside in re.findall(r"([^{}]+)\{([^{}]*)\}", plain)
        if selector in (part.strip() for part in head.split(","))
    ]


def test_the_new_places_show_their_focus_and_their_links_are_as_tall_as_a_control() -> None:
    """Each new place a link or a response focuses takes the cue the evidence has, a tint
    and a bar with no outline around it, and a problem line widens its own edge; the links
    inside the details' sentences keep their line and take a control's height; the jump is
    as tall as a button. The browser shows what these give; here the rules are pinned."""
    css = (REPOSITORY_ROOT / "blossom" / "static" / "blossom.css").read_text(encoding="utf-8")
    for selector, cue in (
        (".evidence:focus", ARRIVAL_CUE),
        ("#instructions:focus", ARRIVAL_CUE),
        ("#waiting-instructions:focus", ARRIVAL_CUE),
        ("#update-or-turn-in:focus", ARRIVAL_CUE),
        ("#turning-it-in:focus", ARRIVAL_CUE),
        ("#problem-summary:focus", PROBLEM_CUE),
        ('.detail .problem[tabindex="-1"]:focus', PROBLEM_CUE),
    ):
        assert declared_for(selector) == [cue], selector
    for selector in (
        ".assignment.detail .confidence a",
        "#problem-summary a",
        ".detail-notes a",
        ".school-instructions .source a",
    ):
        found = rules_for(css, selector)
        assert any(
            "display: inline-block;" in inside
            and "padding: 0.8rem 0;" in inside
            and "margin: -0.8rem 0;" in inside
            for inside in found
        ), selector
    assert any(
        "display: inline-flex;" in inside and "min-height: 2.75rem;" in inside
        for inside in rules_for(css, ".jump a")
    )
