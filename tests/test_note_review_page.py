# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Gerardo Bodegas Martinez
"""Teachers' notes on the review page of a paste: tasks to select, shown by kind.

Every case runs the path a parent's paste takes: the reader, the review page
and its own form, and the save. Homework read from a note is a card with a
tick, selected from the start; a note that may be homework and a note that says
no homework is assigned are shown apart and not selected; each has its source
words beside it; and no line of the text goes unshown. A paste with no note
gives the page pinned here byte for byte, with no tick and no note section.

Every card and note here is made up, in the shapes the school's weekly summary
writes them.
"""

import hashlib
import html
import pathlib
import re

import pytest
from fastapi.testclient import TestClient

from blossom.app import create_app
from tests.support import SAME_ORIGIN, fixture_settings, store_of

NOTED = """Homework for Wren
11/02/2026 - Monday
08 Geometry - Due: Textbook page 12:
Humanities -
Study for the vocabulary quiz on Thursday. Map project due on Wednesday.

Science -
Reading log due today

08 Geometry -
Quiz on unit two

11/03/2026 - Tuesday
Religion -
Short essay Due.
Religion - Due: Short essay:
Write 250 words on your question.

Music -
Unit test today. No additional Music homework assigned.

Art -
"""
"""Two days of the weekly summary: two cards, one with an instruction of its own and a
note above it that only names it; three notes that are homework, one that may be, one that
says no homework is assigned; and a heading with nothing under it."""

NO_NOTE = """Homework for Wren

* 11/02/2026 - Monday
08 Geometry - Assigned: Practice set 4: (Due:11/06/2026)
Show each step.
* 11/03/2026 - Tuesday
Humanities - Due: Reading journal:
Spanish - Assigned: Oral quiz: (Due:11/10/2026)
"""
"""A week of cards with no teacher's note."""

ONE_UNCLEAR_NOTE = "Homework for Wren\n11/02/2026 - Monday\n08 Geometry -\nQuiz on unit two\n"
"""A day whose only content is a note that may be homework."""

MAY_BE = "Notes that may be homework"
NONE_ASSIGNED = "Notes with no homework"
NOT_SURE = "Blossom isn&#39;t sure these are homework, so none is selected. Tick one to save it."
NONE_SAID = (
    "The school&#39;s note says no homework is assigned. "
    "Nothing from these is saved unless one is ticked."
)
NOTHING_SELECTED = "No assignment is selected to save."

FIRST_VIEW = "8ad79b0580d5042339c3f292ad1185afad67e49c47c31fad6d60e62a54b4a61b"
"""The no-note review page before any save, as a hash of its HTML with the form's signature
and the moment of reading left out."""
SAVED_VIEW = "da051b719cc0e3ad6257b79b676a2ffb0b79fd1035428a57dc932bd483f86b36"
"""The same text read again once it is saved, hashed the same way."""


def client_in(tmp_path: pathlib.Path) -> TestClient:
    """The application on a store of its own, as its pages reach it."""
    settings = fixture_settings(
        BLOSSOM_TODAY="2026-11-03",
        BLOSSOM_FIXTURE_PATH="",
        BLOSSOM_DATABASE_PATH=str(tmp_path / "blossom.sqlite3"),
        BLOSSOM_CHECKPOINT_PATH=str(tmp_path / "checkpoints.sqlite3"),
        BLOSSOM_TRACE_PATH=str(tmp_path / "traces.sqlite3"),
    )
    return TestClient(create_app(settings), follow_redirects=False, headers=SAME_ORIGIN)


def ticked_form(page: str) -> dict[str, str]:
    """The review form as a browser sends it back untouched: the hidden fields, each
    select's chosen option, each ticked box of a note, and the text."""
    fields = {
        name: html.unescape(value)
        for name, value in re.findall(r'<input type="hidden" name="([^"]+)" value="([^"]*)">', page)
    }
    for name, body in re.findall(r'<select name="([^"]+)"[^>]*>(.*?)</select>', page, re.S):
        chosen = re.search(r'<option value="([^"]+)" selected>', body)
        assert chosen is not None, name
        fields[name] = chosen.group(1)
    for name in re.findall(r'<input type="checkbox" name="(take-\d+)" value="1" checked>', page):
        fields[name] = "1"
    text = re.search(r'<textarea name="text" hidden>(.*?)</textarea>', page, re.S)
    assert text is not None
    fields["text"] = html.unescape(text.group(1))
    return fields


def hashed(page: str) -> str:
    """A page's HTML as a hash, without the two values that differ between two readings of
    the same text: the form's signature and the moment of reading."""
    for name in ("made_with", "read_at"):
        page, found = re.subn(rf'name="{name}" value="[^"]*"', f'name="{name}"', page)
        assert found == 1, name
    return hashlib.sha256(page.encode("utf-8")).hexdigest()


def sections(page: str) -> dict[str, str]:
    """Each section of cards on a page under its heading, in the order shown."""
    found = re.findall(
        r'<section class="review-week">\s*<h2>([^<]*)</h2>(.*?)</section>', page, re.S
    )
    return {html.unescape(label): body for label, body in found}


def card(page: str, title: str) -> str:
    """The card on a page with this title, from its opening to its end."""
    found: list[str] = re.findall(
        rf"<article [^>]*>(?:(?!</article>).)*?<h2>{re.escape(title)}</h2>.*?</article>",
        page,
        re.S,
    )
    assert len(found) == 1, title
    return found[0]


def titles(body: str) -> list[str]:
    """The titles of the cards in a part of a page, in the order shown."""
    return [
        html.unescape(title)
        for title in re.findall(r"<article [^>]*>.*?<h2>([^<]*)</h2>", body, re.S)
    ]


def numbers(page: str, pattern: str) -> set[int]:
    """The card numbers a pattern with one group finds on a page."""
    return {int(number) for number in re.findall(pattern, page)}


def test_a_paste_with_no_note_gives_the_pinned_page_byte_for_byte(tmp_path: pathlib.Path) -> None:
    """A text with no teacher's note gives the review page pinned here, byte for byte,
    before the save and after it: no tick, no note section, and the counts of its cards."""
    with client_in(tmp_path) as client:
        page = client.post("/parent/inbox/read", data={"text": NO_NOTE}).text
        kept = client.post("/parent/inbox/keep", data=ticked_form(page))
        again = client.post("/parent/inbox/read", data={"text": NO_NOTE}).text

    assert kept.status_code == 303
    assert kept.headers["location"].endswith("added=3&updated=0&unchanged=0")
    for shown in (page, again):
        for words in ("offered-", "take-", MAY_BE, NONE_ASSIGNED, NOTHING_SELECTED, "also says"):
            assert words not in shown
    assert "3 assignments across 2 weeks: 3 new, 0 updates, 0 already saved." in " ".join(
        page.split()
    )
    assert (hashed(page), hashed(again)) == (FIRST_VIEW, SAVED_VIEW)


def test_the_first_view_selects_the_homework_notes_and_no_other(tmp_path: pathlib.Path) -> None:
    """Each reading made from a note is offered with a tick and each card without one. The
    ticks of the three homework notes are set, dated or not; the note that may be homework
    and the note that says none is assigned are not selected."""
    with client_in(tmp_path) as client:
        page = client.post("/parent/inbox/read", data={"text": NOTED}).text

    assert numbers(page, r'<input type="hidden" name="offered-(\d+)" value="1">') == {2, 3, 4, 5, 6}
    assert numbers(page, r'<input type="checkbox" name="take-(\d+)" value="1"') == {2, 3, 4, 5, 6}
    assert numbers(page, r'<input type="checkbox" name="take-(\d+)" value="1" checked>') == {
        2,
        3,
        4,
    }
    for title, key in (
        ("Study for the vocabulary quiz on Thursday", 2),
        ("Map project", 3),
        ("Reading log", 4),
        ("Quiz on unit two", 5),
    ):
        assert f'name="take-{key}"' in card(page, title)
    assert 'name="take-' not in card(page, "Textbook page 12")
    assert 'name="take-' not in card(page, "Short essay")


def test_an_untouched_review_saves_the_homework_notes_in_one_press(tmp_path: pathlib.Path) -> None:
    """The form sent back as shown saves the two cards and the three homework notes with one
    press; the note that may be homework and the note that says none is assigned are not
    written."""
    with client_in(tmp_path) as client:
        page = client.post("/parent/inbox/read", data={"text": NOTED}).text
        kept = client.post("/parent/inbox/keep", data=ticked_form(page))
        rows = {row.title: row for row in store_of(client).all_assignments()}

    assert kept.status_code == 303
    assert kept.headers["location"].endswith("added=5&updated=0&unchanged=0")
    assert set(rows) == {
        "Textbook page 12",
        "Short essay",
        "Study for the vocabulary quiz on Thursday",
        "Map project",
        "Reading log",
    }
    assert {row.course for row in rows.values()} == {
        "08 Geometry",
        "Religion",
        "Humanities",
        "Science",
    }
    assert rows["Reading log"].due_date is not None
    assert rows["Study for the vocabulary quiz on Thursday"].due_date is None


def test_the_three_kinds_of_note_are_under_their_headings_in_order(tmp_path: pathlib.Path) -> None:
    """Homework read from a note is with the cards, by week or with no due date; after the
    weeks come the notes that may be homework and then the notes with no homework, each
    under its heading and its helper line."""
    with client_in(tmp_path) as client:
        page = client.post("/parent/inbox/read", data={"text": NOTED}).text

    shown = sections(page)

    assert list(shown) == [
        "Week of Monday, November 2, 2026",
        "No due date yet",
        MAY_BE,
        NONE_ASSIGNED,
    ]
    assert titles(shown["Week of Monday, November 2, 2026"]) == [
        "Textbook page 12",
        "Reading log",
        "Short essay",
        "Map project",
    ]
    assert titles(shown["No due date yet"]) == ["Study for the vocabulary quiz on Thursday"]
    assert titles(shown[MAY_BE]) == ["Quiz on unit two"]
    assert len(titles(shown[NONE_ASSIGNED])) == 1
    assert "Unit test today. No additional Music homework assigned." in shown[NONE_ASSIGNED]
    assert f'<p class="note"> {NOT_SURE} </p>' in " ".join(shown[MAY_BE].split())
    assert f'<p class="note"> {NONE_SAID} </p>' in " ".join(shown[NONE_ASSIGNED].split())
    assert page.count(NOT_SURE) == page.count(NONE_SAID) == 1
    assert page.index('id="needs-review"') < page.index("Week of Monday")


def test_each_note_has_its_words_on_its_card_and_none_is_listed_for_review(
    tmp_path: pathlib.Path,
) -> None:
    """A note's source words are on its card, with the school day of the note they were read
    in; the text that needs review lists the heading with nothing under it and no line of a
    note that became a task."""
    with client_in(tmp_path) as client:
        page = client.post("/parent/inbox/read", data={"text": NOTED}).text

    review = page.split('id="needs-review"', 1)[1].split(">Edit the text</button>", 1)[0]
    for title, words, day in (
        (
            "Study for the vocabulary quiz on Thursday",
            "Study for the vocabulary quiz on Thursday.",
            2,
        ),
        ("Map project", "Map project due on Wednesday.", 2),
        ("Reading log", "Reading log due today", 2),
        ("Quiz on unit two", "Quiz on unit two", 2),
    ):
        shown = card(page, title)
        assert f'From the school: <q class="authored-text">{words}</q>' in shown
        assert f"Read in the school's note for November {day}." in shown
        assert "school's card" not in shown
    assert "Art -" in review
    for line in ("Humanities -", "vocabulary quiz", "Map project", "Science -", "Reading log"):
        assert line not in html.unescape(review)
    for line in ("08 Geometry -", "Quiz on unit", "Religion -", "Short essay Due", "Music -"):
        assert line not in html.unescape(review)
    assert "Unit test today" not in review


def test_a_note_that_only_names_a_card_is_a_line_on_that_card(tmp_path: pathlib.Path) -> None:
    """The note above the card of the same name and day is no card of its own: its words
    are a line on that card, which keeps its own instruction as the school's card gave it."""
    with client_in(tmp_path) as client:
        page = client.post("/parent/inbox/read", data={"text": NOTED}).text

    shown = card(page, "Short essay")

    assert (
        '<p class="source">The teacher\'s note for November 3 also says: '
        '<q class="authored-text">Short essay Due.</q></p>'
    ) in shown
    assert (
        'From the school: <q class="authored-text">Write 250 words on your question.</q>' in shown
    )
    assert "Read on the school's card for November 3." in shown
    assert "school's note" not in shown
    assert page.count("also says") == 1
    assert len(re.findall(r"<h2>Short essay</h2>", page)) == 1


def test_a_note_not_selected_has_no_type_and_counts_in_no_total(tmp_path: pathlib.Path) -> None:
    """The page counts the selected readings only, in its summary, in each week and on the
    save button. A card not selected says so, offers no type, and keeps its tick; a selected
    note offers a type as a card does, and its tick names it."""
    with client_in(tmp_path) as client:
        page = client.post("/parent/inbox/read", data={"text": NOTED}).text

    said = " ".join(page.split())
    unclear = card(page, "Quiz on unit two")

    assert (
        "5 assignments across 1 week, 1 with no due date yet: 5 new, 0 updates, 0 already saved."
    ) in said
    assert "4 assignments: 4 new, 0 updates, 0 already saved." in said
    assert "1 assignment: 1 new, 0 updates, 0 already saved." in said
    assert "0 assignments" not in said
    assert ">Save 5 assignments</button>" in said
    assert '<span class="pill">Not selected</span>' in unclear
    assert "Not selected. Nothing from this note is saved." in unclear
    assert "<select" not in unclear
    assert 'name="kind-5"' not in page
    assert '<select name="kind-4"' in card(page, "Reading log")
    assert (
        '<label><input type="checkbox" name="take-4" value="1" checked> '
        '<span>Save this as homework<span class="visually-hidden"> for Reading log, Science</span>'
        "</span></label>"
    ) in card(page, "Reading log")
    assert NOTHING_SELECTED not in page


def test_a_paste_whose_only_note_may_be_homework_says_none_is_selected(
    tmp_path: pathlib.Path,
) -> None:
    """With no reading selected the page says so in place of its counts, shows the note
    under its heading with its tick, and offers no save."""
    with client_in(tmp_path) as client:
        page = client.post("/parent/inbox/read", data={"text": ONE_UNCLEAR_NOTE}).text

    assert f'<p class="empty">{NOTHING_SELECTED}</p>' in page
    assert "Nothing in the text was read as an assignment." not in page
    assert list(sections(page)) == [MAY_BE]
    assert '<input type="checkbox" name="take-0" value="1">' in page
    assert 'class="primary"' not in page
    assert 'id="needs-review"' not in page


def test_a_saved_note_read_again_has_its_words_on_its_card(tmp_path: pathlib.Path) -> None:
    """The same text read again once its homework notes are saved: each saved note is a card
    that is saved already, with its tick, and its source words are on it with the school
    day of the note, so no line of the text goes unshown."""
    with client_in(tmp_path) as client:
        page = client.post("/parent/inbox/read", data={"text": NOTED}).text
        client.post("/parent/inbox/keep", data=ticked_form(page))
        again = client.post("/parent/inbox/read", data={"text": NOTED}).text

    review = again.split('id="needs-review"', 1)[1].split(">Edit the text</button>", 1)[0]
    for title, words in (
        ("Study for the vocabulary quiz on Thursday", "Study for the vocabulary quiz on Thursday."),
        ("Map project", "Map project due on Wednesday."),
        ("Reading log", "Reading log due today"),
    ):
        shown = card(again, title)
        assert '<span class="pill">Already saved</span>' in shown
        assert f'From the school: <q class="authored-text">{words}</q>' in shown
        assert "Read in the school's note for November 2." in shown
        assert shown.count(words) == 1
        assert words not in html.unescape(review)
    assert numbers(again, r'<input type="checkbox" name="take-(\d+)" value="1" checked>') == {
        2,
        3,
        4,
    }
    assert (
        "5 assignments across 1 week, 1 with no due date yet: 0 new, 0 updates, 5 already saved."
        in " ".join(again.split())
    )


def test_a_find_press_shows_the_ticks_as_they_were_sent(tmp_path: pathlib.Path) -> None:
    """A press that only shows the review again reads the ticks the form carries: a note
    ticked on the page comes back selected and one unticked comes back not selected, each
    under the heading of its kind, and nothing is saved."""
    with client_in(tmp_path) as client:
        page = client.post("/parent/inbox/read", data={"text": NOTED}).text
        form = ticked_form(page) | {"take-5": "1", "search-0": "textbook", "find": "0"}
        del form["take-4"]
        found = client.post("/parent/inbox/find", data=form)
        rows = store_of(client).all_assignments()

    shown = sections(found.text)

    assert found.status_code == 200
    assert rows == []
    assert numbers(found.text, r'<input type="checkbox" name="take-(\d+)" value="1" checked>') == {
        2,
        3,
        5,
    }
    assert numbers(found.text, r'<input type="hidden" name="offered-(\d+)" value="1">') == {
        2,
        3,
        4,
        5,
        6,
    }
    assert titles(shown[MAY_BE]) == ["Quiz on unit two"]
    assert "Reading log" in titles(shown["Week of Monday, November 2, 2026"])
    assert '<span class="pill">Not selected</span>' in card(found.text, "Reading log")
    assert "<select" not in card(found.text, "Reading log")
    assert '<select name="kind-5"' in card(found.text, "Quiz on unit two")
    assert "3 assignments: 3 new, 0 updates, 0 already saved." in " ".join(found.text.split())


def test_a_note_that_says_more_than_its_cards_name_is_listed_once_on_that_card(
    tmp_path: pathlib.Path,
) -> None:
    """A note above the card of the same name and day whose words say more is one more
    instruction of that card, so the card asks which of the two apply. Each is listed once
    with where it was read, the card's own on the school's card and the note's in the
    school's note, and the note is not repeated as a line of the teacher's note."""
    text = NOTED.replace("Short essay Due.", "Short essay due today. Write it in ink.")
    with client_in(tmp_path) as client:
        page = client.post("/parent/inbox/read", data={"text": text}).text

    shown = card(page, "Short essay")
    listed = re.findall(
        r'<q class="authored-text">([^<]*)</q> <span class="source">([^<]*)</span>', shown
    )

    assert "The school's instructions for Short essay: which apply now?" in shown
    assert listed == [
        (
            "Write 250 words on your question.",
            "New in this text. Read on the school's card for November 3.",
        ),
        (
            "Short essay due today. Write it in ink.",
            "New in this text. Read in the school's note for November 3.",
        ),
    ]
    assert "also says" not in page
    assert 'name="take-' not in shown
    assert len(re.findall(r"<h2>Short essay</h2>", page)) == 1


TWICE_NOTED = """Homework for Wren
11/02/2026 - Monday
Science -
Reading log due Friday

11/04/2026 - Wednesday
Science -
Reading log due Friday. Show your work.
"""
"""Two notes on two days that name the same work in different words and settle the same
date, so the later one asks whether it is the same assignment or new work."""

SELECTIONS_CHANGE = (
    "Your selections change what saving would do for a card below, so nothing was saved. "
    "Check the review, then save."
)
REVIEW_CHANGES = (
    '<button type="submit" class="secondary" formaction="/parent/inbox/find" formnovalidate '
    'name="recheck" value="1">Review changes</button>'
)
REVIEW_CHANGES_HELPER = (
    "Review changes shows this review again with your selections. Nothing is saved until you save."
)
TICKED = r'<input type="checkbox" name="take-(\d+)" value="1" checked>'


def test_a_note_ticked_by_the_family_saves_in_one_press(tmp_path: pathlib.Path) -> None:
    """A note shown unticked says what selecting it does. Ticked on the page as shown and
    saved, it is written with the rest in one press."""
    with client_in(tmp_path) as client:
        page = client.post("/parent/inbox/read", data={"text": NOTED}).text
        kept = client.post("/parent/inbox/keep", data=ticked_form(page) | {"take-5": "1"})
        rows = {row.title for row in store_of(client).all_assignments()}

    assert '<p class="helper">If selected: New.</p>' in card(page, "Quiz on unit two")
    assert page.count("If selected: ") == 2
    assert "If selected" not in card(page, "Reading log")
    assert kept.status_code == 303
    assert kept.headers["location"].endswith("added=6&updated=0&unchanged=0")
    assert "Quiz on unit two" in rows
    assert len(rows) == 6


def test_a_note_unticked_by_the_family_is_not_written_in_one_press(tmp_path: pathlib.Path) -> None:
    """A homework note unticked on the page as shown is left out of the save, which writes
    the rest in one press."""
    with client_in(tmp_path) as client:
        page = client.post("/parent/inbox/read", data={"text": NOTED}).text
        form = ticked_form(page)
        del form["take-4"]
        kept = client.post("/parent/inbox/keep", data=form)
        rows = {row.title for row in store_of(client).all_assignments()}

    assert kept.status_code == 303
    assert kept.headers["location"].endswith("added=4&updated=0&unchanged=0")
    assert "Reading log" not in rows
    assert len(rows) == 4


def test_a_tick_on_a_form_the_page_did_not_sign_writes_nothing(tmp_path: pathlib.Path) -> None:
    """A form without the page's signature that ticks a note writes nothing: the review comes
    back with the ticks as they were sent."""
    with client_in(tmp_path) as client:
        page = client.post("/parent/inbox/read", data={"text": NOTED}).text
        form = ticked_form(page) | {"take-5": "1"}
        del form["made_with"]
        kept = client.post("/parent/inbox/keep", data=form)
        rows = store_of(client).all_assignments()

    assert kept.status_code == 409
    assert rows == []
    assert numbers(kept.text, TICKED) == {2, 3, 4, 5}


def test_review_changes_writes_nothing_and_returns_the_ticks_as_sent(
    tmp_path: pathlib.Path,
) -> None:
    """The review of a paste with notes offers Review changes beside Save, with a line that
    says what it does. Pressed, it shows the review again with the ticks as sent and writes
    nothing; the page it returns saves in one press. A paste with no note is not offered it."""
    with client_in(tmp_path) as client:
        plain = client.post("/parent/inbox/read", data={"text": NO_NOTE}).text
        page = client.post("/parent/inbox/read", data={"text": NOTED}).text
        form = ticked_form(page) | {"take-5": "1", "recheck": "1"}
        del form["take-4"]
        again = client.post("/parent/inbox/find", data=form)
        unwritten = store_of(client).all_assignments()
        kept = client.post("/parent/inbox/keep", data=ticked_form(again.text))
        rows = {row.title for row in store_of(client).all_assignments()}

    assert "recheck" not in plain
    assert "Review changes" not in plain
    for shown in (page, again.text):
        assert shown.count(REVIEW_CHANGES) == 1
        assert shown.count(f'<p class="helper">{REVIEW_CHANGES_HELPER}</p>') == 1
    assert again.status_code == 200
    assert unwritten == []
    assert numbers(again.text, TICKED) == {2, 3, 5}
    assert '<span class="pill">Not selected</span>' in card(again.text, "Reading log")
    assert '<p class="helper">If selected: New.</p>' in card(again.text, "Reading log")
    assert "If selected" not in card(again.text, "Quiz on unit two")
    assert ">Save 5 assignments</button>" in again.text
    assert kept.status_code == 303
    assert kept.headers["location"].endswith("added=5&updated=0&unchanged=0")
    assert "Quiz on unit two" in rows
    assert "Reading log" not in rows


def test_a_paste_with_nothing_selected_offers_review_changes(tmp_path: pathlib.Path) -> None:
    """With no reading selected the page offers no save, and Review changes is the press to
    make: a note ticked and reviewed comes back selected, with the save offered."""
    with client_in(tmp_path) as client:
        page = client.post("/parent/inbox/read", data={"text": ONE_UNCLEAR_NOTE}).text
        form = ticked_form(page) | {"take-0": "1", "recheck": "1"}
        again = client.post("/parent/inbox/find", data=form)
        rows = store_of(client).all_assignments()

    assert page.count(REVIEW_CHANGES) == 1
    assert again.status_code == 200
    assert rows == []
    assert numbers(again.text, TICKED) == {0}
    assert ">Save 1 assignment</button>" in again.text


def test_a_selection_that_changes_another_card_saves_nothing_and_says_why(
    tmp_path: pathlib.Path,
) -> None:
    """Two notes name the same work, and the later one asks whether it is the same
    assignment. With the earlier one unticked the later one would be saved with no question,
    which its card did not say: nothing is saved, and the page says why, first and with the
    focus, with the ticks as sent."""
    with client_in(tmp_path) as client:
        page = client.post("/parent/inbox/read", data={"text": TWICE_NOTED}).text
        form = ticked_form(page)
        del form["take-0"]
        kept = client.post("/parent/inbox/keep", data=form)
        rows = store_of(client).all_assignments()

    assert numbers(page, TICKED) == {0, 1}
    assert page.count('<span class="pill">Needs your answer</span>') == 1
    assert kept.status_code == 200
    assert rows == []
    assert (
        '<p class="problem" role="alert" id="problem-summary" tabindex="-1" autofocus>'
        f"{SELECTIONS_CHANGE}</p>"
    ) in kept.text
    assert numbers(kept.text, TICKED) == {1}


def test_the_same_review_saved_twice_writes_once(tmp_path: pathlib.Path) -> None:
    """The form of one review sent twice, as a double press sends it: the second save finds
    each note in the row the page named, writes nothing, and says the five are unchanged."""
    with client_in(tmp_path) as client:
        page = client.post("/parent/inbox/read", data={"text": NOTED}).text
        first = client.post("/parent/inbox/keep", data=ticked_form(page))
        second = client.post("/parent/inbox/keep", data=ticked_form(page))
        rows = store_of(client).all_assignments()

    assert first.status_code == second.status_code == 303
    assert second.headers["location"].endswith("added=0&updated=0&unchanged=5")
    assert len(rows) == 5


def test_a_note_card_answered_on_the_page_saves_in_one_press(tmp_path: pathlib.Path) -> None:
    """A note's card that asks whether it is the same assignment or new work, answered on
    the page as shown, is saved with the answer in one press."""
    with client_in(tmp_path) as client:
        page = client.post("/parent/inbox/read", data={"text": TWICE_NOTED}).text
        kept = client.post("/parent/inbox/keep", data=ticked_form(page) | {"occurrence-1": "new"})
        rows = store_of(client).all_assignments()

    assert 'name="occurrence-1" value="new"' in page
    assert kept.status_code == 303
    assert kept.headers["location"].endswith("added=2&updated=0&unchanged=0")
    assert [row.title for row in rows] == ["Reading log", "Reading log"]


WITH_A_QUESTION = TWICE_NOTED + "\n11/05/2026 - Thursday\n08 Geometry -\nQuiz on unit two\n"
"""The two notes of one name, the later one asking which it is, and a note that may be
homework on a third day."""


def claims_damaged(client: TestClient) -> None:
    """Every claim about a date on record with its flag made into what the store never
    writes, so the strict readers refuse it."""
    connection = store_of(client)._connection
    connection.execute("UPDATE date_claims SET active = 2")
    connection.commit()


@pytest.mark.parametrize(
    ("text", "sent", "path", "status", "ticked"),
    [
        (NOTED, {"take-5": "1", "take-4": None, "link-0-0": "1"}, "keep", 422, {2, 3, 5}),
        (NOTED, {"take-5": "1", "take-4": None, "made_with": "0.0"}, "keep", 409, {2, 3, 5}),
        (WITH_A_QUESTION, {"take-2": "1"}, "keep", 200, {0, 1, 2}),
        (WITH_A_QUESTION, {"take-2": "1", "take-0": None}, "keep", 200, {1, 2}),
        (NOTED, {"take-5": "1", "take-4": None, "find": "99"}, "find", 422, {2, 3, 5}),
    ],
    ids=["unreadable-links", "unsigned", "open-question", "selections-change", "refused-press"],
)
def test_a_page_handed_back_carries_the_ticks_as_sent(
    tmp_path: pathlib.Path,
    text: str,
    sent: dict[str, str | None],
    path: str,
    status: int,
    ticked: set[int],
) -> None:
    """A save or a press that is refused, or a save handed back for a question, writes
    nothing and shows the review again with each note ticked or not as the form sent it."""
    with client_in(tmp_path) as client:
        page = client.post("/parent/inbox/read", data={"text": text}).text
        form = ticked_form(page)
        for name, value in sent.items():
            if value is None:
                del form[name]
            else:
                form[name] = value
        answer = client.post(f"/parent/inbox/{path}", data=form)
        rows = store_of(client).all_assignments()

    assert answer.status_code == status
    assert rows == []
    assert numbers(answer.text, TICKED) == ticked
    assert numbers(answer.text, r'<input type="hidden" name="offered-(\d+)" value="1">') == (
        numbers(page, r'<input type="hidden" name="offered-(\d+)" value="1">')
    )


def test_a_note_folded_into_another_card_keeps_its_tick(tmp_path: pathlib.Path) -> None:
    """A note answered as the same assignment is folded into the card for it and is not
    shown; the page sends its tick back with its answer. The card it is folded into asks
    which of the two notes' words apply, and with that answered the page saves in one
    press, as one row."""
    with client_in(tmp_path) as client:
        page = client.post("/parent/inbox/read", data={"text": TWICE_NOTED}).text
        form = ticked_form(page) | {"occurrence-1": "update", "recheck": "1"}
        again = client.post("/parent/inbox/find", data=form)
        kept = client.post(
            "/parent/inbox/keep",
            data=ticked_form(again.text) | {"apply-0-0": "1", "apply-0-1": "1"},
        )
        rows = store_of(client).all_assignments()

    assert again.status_code == 200
    assert "The school's instructions for Reading log: which apply now?" in html.unescape(
        again.text
    )
    assert '<input type="hidden" name="folded-1" value="0">' in again.text
    assert '<input type="hidden" name="offered-1" value="1">' in again.text
    assert '<input type="hidden" name="take-1" value="1">' in again.text
    assert kept.status_code == 303
    assert [row.title for row in rows] == ["Reading log"]


def test_the_page_that_reads_no_store_lists_each_tick(tmp_path: pathlib.Path) -> None:
    """When a claim on record cannot be read, the page that reads no store says of each
    note whether it was selected to save: as the form sent it for a save and for a press,
    and as the text selects it from the start for a first reading."""
    with client_in(tmp_path) as client:
        page = client.post("/parent/inbox/read", data={"text": NOTED}).text
        form = ticked_form(page)
        assert client.post("/parent/inbox/keep", data=form).status_code == 303
        claims_damaged(client)
        form["take-5"] = "1"
        del form["take-4"]
        saved = client.post("/parent/inbox/keep", data=form)
        pressed = client.post("/parent/inbox/find", data=form | {"recheck": "1"})
        first = client.post("/parent/inbox/read", data={"text": NOTED})
        plain = client.post("/parent/inbox/read", data={"text": NO_NOTE})

    for answer in (saved, pressed):
        assert answer.status_code == 500
        for key, said in ((2, "selected to save"), (4, "not selected"), (5, "selected to save")):
            assert f"Card {key}: no answer about the assignment; {said}." in answer.text
        assert "Card 6: no answer about the assignment; not selected." in answer.text
        assert "Card 0:" not in answer.text
    assert first.status_code == 500
    assert "Card 4: no answer about the assignment; selected to save." in first.text
    assert "Card 5: no answer about the assignment; not selected." in first.text
    assert plain.status_code == 200


UNDATED_AGAIN = (
    "Homework for Wren\n11/02/2026 - Monday\n"
    "Spanish - Assigned: Oral quiz: (Due:11/10/2026)\n"
    "11/03/2026 - Tuesday\nSpanish -\nOral quiz Due.\n"
)
"""A card on Monday, and on Tuesday a note that names it and settles no date."""


def test_a_notes_question_offers_the_same_assignment_in_words_true_of_a_note(
    tmp_path: pathlib.Path,
) -> None:
    """A note's card that asks which it is offers the same assignment without saying a due
    date changed: a note that settles a date says the date the assignment takes, and a note
    that settles none says no more."""
    with client_in(tmp_path) as client:
        dated = client.post("/parent/inbox/read", data={"text": TWICE_NOTED}).text
        undated = client.post("/parent/inbox/read", data={"text": UNDATED_AGAIN}).text

    assert (
        '<label><input type="radio" name="occurrence-1" value="update"> '
        "The same assignment; its due date becomes Friday, November 6, 2026</label>"
    ) in dated
    assert (
        '<label><input type="radio" name="occurrence-1" value="update"> The same assignment</label>'
    ) in undated
    for page in (dated, undated):
        assert page.count('value="update">') == 1
        assert "its due date changed</label>" not in page
        assert '<input type="radio" name="occurrence-1" value="new"> New work under' in page
