# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Gerardo Bodegas Martinez
"""Teachers' notes on the review page of a paste: tasks to select, shown by kind.

Every case runs the path a parent's paste takes: the reader, the review page
and its own form, and the save. Every reading made from a note is a card with a
tick, unselected from the start; homework read from a note is with the cards, and
a note that may be homework and a note that says no homework is assigned are
shown apart; each has its source
words beside it; and no line of the text goes unshown. A paste with no note
gives the page pinned here byte for byte, with no tick and no note section.

Every card and note here is made up, in the shapes the school's weekly summary
writes them.
"""

import datetime
import hashlib
import html
import json
import pathlib
import re
from html.parser import HTMLParser
from urllib.parse import urlencode

import pytest
from fastapi.testclient import TestClient

from blossom.app import create_app
from blossom.reconciliation import SourceChannel
from blossom.routes.inbox import REVIEW_FIELDS, review_field
from tests.support import (
    SAME_ORIGIN,
    Answer,
    fixture_settings,
    homework_from_a_note,
    store_of,
    waiting_note,
)

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
NOT_SURE = "These start unselected. Select any that are homework."
NONE_SAID = "The school&#39;s note says no homework is assigned. These start unselected."
NOTHING_SELECTED = "No assignment is selected to save."
SAVE_SELECTED = '<button type="submit" class="primary">Save selected assignments</button>'

FIRST_VIEW = "6207794f6b697224d87ebab6b99a63c49c5df1de6193999344f5e6b9f38575d2"
"""The no-note review page before any save, as a hash of its HTML with the form's signature,
the moment of reading and the tag of the stylesheet and the script left out."""
SAVED_VIEW = "c0e420da2293eeb951c4d45834dd3f79915e0e5cc749e2474542d1611add5d90"
"""The same text read again once it is saved, hashed the same way."""


def client_in(tmp_path: pathlib.Path, today: str = "2026-11-03") -> TestClient:
    """The application on a store of its own, as its pages reach it."""
    settings = fixture_settings(
        BLOSSOM_TODAY=today,
        BLOSSOM_FIXTURE_PATH="",
        BLOSSOM_DATABASE_PATH=str(tmp_path / "blossom.sqlite3"),
        BLOSSOM_CHECKPOINT_PATH=str(tmp_path / "checkpoints.sqlite3"),
        BLOSSOM_TRACE_PATH=str(tmp_path / "traces.sqlite3"),
    )
    return TestClient(create_app(settings), follow_redirects=False, headers=SAME_ORIGIN)


def ticked_form(page: str) -> dict[str, str]:
    """The review form as a browser sends it back untouched: the hidden fields, each
    select's chosen option, each ticked box of a note or of a note joined to a card, each
    note's title box, chosen due date and date box, and the text."""
    fields = {
        name: html.unescape(value)
        for name, value in re.findall(r'<input type="hidden" name="([^"]+)" value="([^"]*)">', page)
    }
    for name, body in re.findall(r'<select name="([^"]+)"[^>]*>(.*?)</select>', page, re.S):
        chosen = re.search(r'<option value="([^"]*)" selected>', body)
        assert chosen is not None, name
        fields[name] = chosen.group(1)
    for name in re.findall(
        r'<input type="checkbox" name="(take-\d+|join-\d+-\d+)" value="1" checked>', page
    ):
        fields[name] = "1"
    for name, value in re.findall(
        r'<input type="(?:text|date)" name="((?:title|when_other)-\d+)" value="([^"]*)"', page
    ):
        fields[name] = html.unescape(value)
    for name, value in re.findall(
        r'<input type="radio" name="(when-\d+)" value="([^"]+)" checked>', page
    ):
        fields[name] = value
    text = re.search(r'<textarea name="text" hidden>(.*?)</textarea>', page, re.S)
    assert text is not None
    fields["text"] = html.unescape(text.group(1))
    return fields


def hashed(page: str) -> str:
    """A page's HTML as a hash, without the values that differ between two readings of the
    same text or with the stylesheet's and the script's own bytes: the form's signature, the
    moment of reading, and the tag the page names those two files with."""
    for name in ("made_with", "read_at"):
        page, found = re.subn(rf'name="{name}" value="[^"]*"', f'name="{name}"', page)
        assert found == 1, name
    page, found = re.subn(r'("/static/blossom\.(?:css|js)\?v=)[^"]*"', r'\1"', page)
    assert found == 2
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


def homework_selected(client: TestClient, text: str) -> str:
    """The review of a text with each note read as homework selected by a Review changes
    press, as a family selects them, since every note starts unselected. A text with no such
    note gives its first review."""
    page = client.post("/parent/inbox/read", data={"text": text}).text
    keys = {
        key
        for heading, body in sections(page).items()
        if heading not in (MAY_BE, NONE_ASSIGNED)
        for key in numbers(body, r'name="offered-(\d+)"')
    }
    if not keys:
        return page
    form = ticked_form(page) | {f"take-{key}": "1" for key in keys} | {"recheck": "1"}
    chosen = client.post("/parent/inbox/find", data=form)
    assert chosen.status_code == 200
    return chosen.text


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


def test_the_first_view_offers_each_note_a_tick_and_selects_none(tmp_path: pathlib.Path) -> None:
    """Each reading made from a note is offered with a tick and each card without one. No
    note's tick is set, homework or not, dated or not."""
    with client_in(tmp_path) as client:
        page = client.post("/parent/inbox/read", data={"text": NOTED}).text

    assert numbers(page, r'<input type="hidden" name="offered-(\d+)" value="1">') == {2, 3, 4, 5, 6}
    assert numbers(page, r'<input type="checkbox" name="take-(\d+)" value="1"') == {2, 3, 4, 5, 6}
    assert numbers(page, r'<input type="checkbox" name="take-(\d+)" value="1" checked>') == set()
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
    """The page Review changes returns once the family selects the homework notes, sent back
    as shown, saves the two cards and the three homework notes with one press; the note that
    may be homework and the note that says none is assigned are not written."""
    with client_in(tmp_path) as client:
        page = homework_selected(client, NOTED)
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


TWO_SCHOOL_WEEKS = """Homework for Wren
11/03/2026 - Tuesday
Spanish -
Spelling packet due Friday.

11/09/2026 - Monday
Spanish -
Spelling packet due Friday.
"""
"""The same note on a Tuesday and on the next Monday, six days later."""


def test_the_same_note_in_the_next_school_week_is_a_card_of_its_own(
    tmp_path: pathlib.Path,
) -> None:
    """With both notes selected by Review changes, each listing is a card of its own with the
    school day of its note and its own deadline; the later card asks whether it is the same
    assignment or new work, so a Save of that page as shown writes nothing, and no line of
    either note is listed for review."""
    with client_in(tmp_path, today="2026-11-09") as client:
        page = homework_selected(client, TWO_SCHOOL_WEEKS)
        kept = client.post("/parent/inbox/keep", data=ticked_form(page))
        rows = store_of(client).all_assignments()

    review = page.partition('id="needs-review"')[2].split(">Edit the text</button>", 1)[0]
    cards = re.findall(r"<article [^>]*>.*?</article>", page, re.S)
    packets = [shown for shown in cards if "<h2>Spelling packet</h2>" in shown]

    assert len(packets) == 2
    assert "Read in the school's note for November 3." in packets[0]
    assert "Due Friday, November 6, 2026" in " ".join(packets[0].split())
    assert 'name="occurrence-' not in packets[0]
    assert "Read in the school's note for November 9." in packets[1]
    assert "Due date: Friday, November 13, 2026." in " ".join(packets[1].split())
    assert 'name="occurrence-' in packets[1]
    assert "Spelling packet" not in review
    assert kept.status_code == 200
    assert rows == []


def test_a_notice_that_opens_with_a_listed_word_as_a_noun_starts_unselected(
    tmp_path: pathlib.Path,
) -> None:
    """A notice whose first word is one of the words that open homework, used as a noun, is
    a note that may be homework: it starts unselected."""
    notice = (
        "Homework for Wren\n11/02/2026 - Monday\nScience -\n"
        "Study hall is moved to room 12 on Friday.\n"
    )
    with client_in(tmp_path, today="2026-11-02") as client:
        page = client.post("/parent/inbox/read", data={"text": notice}).text

    shown = sections(page)

    assert titles(shown[MAY_BE]) == ["Study hall is moved to room 12 on Friday"]
    assert numbers(page, r'<input type="checkbox" name="take-(\d+)" value="1"') == {0}
    assert numbers(page, TICKED) == set()


def test_a_no_homework_note_that_also_sets_work_may_be_homework(tmp_path: pathlib.Path) -> None:
    """A note that says no homework and also sets work with a deadline is under the notes
    that may be homework, unselected, never under the notes with no homework."""
    text = (
        "Homework for Wren\n11/02/2026 - Monday\nMath -\n"
        "No homework tonight, but study for the quiz due Friday.\n"
    )
    with client_in(tmp_path, today="2026-11-02") as client:
        page = client.post("/parent/inbox/read", data={"text": text}).text

    shown = sections(page)

    assert titles(shown[MAY_BE]) == ["No homework tonight, but study for the quiz"]
    assert NONE_ASSIGNED not in shown
    assert numbers(page, TICKED) == set()


LISTED_AFTER = """Homework for Wren
11/02/2026 - Monday
Humanities -
Map project due on Wednesday

11/05/2026 - Thursday
Humanities -
Map project due on Wednesday

11/06/2026 - Friday
Humanities -
Map project due on Wednesday
"""
"""One note of the week listed on its Monday and again on two days after its deadline."""


def test_a_note_listed_again_after_its_deadline_says_so_once_for_each_day(
    tmp_path: pathlib.Path,
) -> None:
    """A note listed again after the deadline it settles is one card, which says on a line
    of its own each later day it is listed under; a note listed before its deadline says
    nothing of the kind."""
    with client_in(tmp_path, today="2026-11-06") as client:
        page = client.post("/parent/inbox/read", data={"text": LISTED_AFTER}).text
        plain = client.post("/parent/inbox/read", data={"text": NOTED}).text

    shown = " ".join(card(page, "Map project").split())

    assert "Due Wednesday, November 4, 2026" in shown
    assert (
        '<p class="source">Still listed on Thursday, November 5, 2026, after its due date of '
        "Wednesday, November 4, 2026.</p> "
        '<p class="source">Still listed on Friday, November 6, 2026, after its due date of '
        "Wednesday, November 4, 2026.</p>"
    ) in shown
    assert page.count("Still listed on") == 2
    assert "Still listed on" not in plain


def test_a_note_not_selected_has_no_type_and_counts_in_no_total(tmp_path: pathlib.Path) -> None:
    """The page Review changes returns once the family selects the homework notes counts the
    selected readings only, in its summary and in each week, and offers Save selected
    assignments. A card not selected says so, offers no type, and keeps its tick; a selected
    note offers a type as a card does, and its tick names it."""
    with client_in(tmp_path) as client:
        page = homework_selected(client, NOTED)

    said = " ".join(page.split())
    unclear = card(page, "Quiz on unit two")

    assert (
        "5 assignments across 1 week, 1 with no due date yet: 5 new, 0 updates, 0 already saved."
    ) in said
    assert "4 assignments: 4 new, 0 updates, 0 already saved." in said
    assert "1 assignment: 1 new, 0 updates, 0 already saved." in said
    assert "0 assignments" not in said
    assert SAVE_SELECTED in said
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
    under its heading with its tick, and offers Save."""
    with client_in(tmp_path) as client:
        page = client.post("/parent/inbox/read", data={"text": ONE_UNCLEAR_NOTE}).text

    assert f'<p class="empty">{NOTHING_SELECTED}</p>' in page
    assert "Nothing in the text was read as an assignment." not in page
    assert list(sections(page)) == [MAY_BE]
    assert '<input type="checkbox" name="take-0" value="1">' in page
    assert page.count(SAVE_SELECTED) == 1
    assert 'id="needs-review"' not in page


def test_a_saved_note_read_again_has_its_words_on_its_card(tmp_path: pathlib.Path) -> None:
    """The same text read again once its homework notes are selected by Review changes and
    saved: each saved note is a card that is saved already, with its tick, and its source
    words are on it with the school day of the note, so no line of the text goes unshown."""
    with client_in(tmp_path) as client:
        page = homework_selected(client, NOTED)
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
    """From the page Review changes returns once the family selects the homework notes, a
    press that only shows the review again reads the ticks the form carries: a note ticked
    there comes back selected and one unticked comes back not selected, each under the
    heading of its kind, and nothing is saved."""
    with client_in(tmp_path) as client:
        page = homework_selected(client, NOTED)
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


def test_a_note_that_says_more_than_its_cards_name_adds_nothing_until_selected(
    tmp_path: pathlib.Path,
) -> None:
    """A note above the card of the same name and day whose words say more adds nothing to
    the card as read: the card asks no question about the school's instructions, lists its
    own, and shows the note's words once, as a line of the teacher's note."""
    text = NOTED.replace("Short essay Due.", "Short essay due today. Write it in ink.")
    with client_in(tmp_path) as client:
        page = client.post("/parent/inbox/read", data={"text": text}).text

    shown = card(page, "Short essay")

    assert "which apply now?" not in shown
    assert "Read on the school&#39;s card for November 3." in shown or (
        "Read on the school's card for November 3." in shown
    )
    assert "Read in the school's note" not in html.unescape(shown)
    assert html.unescape(shown).count("Short essay due today. Write it in ink.") == 1
    assert (
        "The teacher's note for November 3 also says: "
        '<q class="authored-text">Short essay due today. Write it in ink.</q>'
    ) in html.unescape(shown)
    assert 'name="take-' not in shown
    assert len(re.findall(r"<h2>Short essay</h2>", page)) == 1


def test_a_selected_note_that_says_more_is_listed_once_on_that_card(
    tmp_path: pathlib.Path,
) -> None:
    """Once the family selects the note above the card of the same name and day, its words
    are one more instruction of that card, so the card asks which of the two apply. Each is
    listed once with where it was read, the card's own on the school's card and the note's
    in the school's note, and the note is not repeated as a line of the teacher's note."""
    text = NOTED.replace("Short essay Due.", "Short essay due today. Write it in ink.")
    with client_in(tmp_path) as client:
        first = client.post("/parent/inbox/read", data={"text": text}).text
        (tick,) = re.findall(r'name="(join-\d+-0)"', card(first, "Short essay"))
        form = ticked_form(first) | {tick: "1", "recheck": "1"}
        page = client.post("/parent/inbox/find", data=form).text

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
    assert joined_ticks(shown) == {tick: True}
    assert len(re.findall(r"<h2>Short essay</h2>", page)) == 1


DATED_APART = NOTED.replace("Short essay Due.", "Short essay due Friday.")
"""The two days, with the note above the Tuesday's card naming a deadline of its own, the
Friday, where the card says the Tuesday."""
JOINED_TICK = r'<input type="checkbox" name="(join-\d+-\d+)" value="1"( checked)?>'
ADD_THIS_NOTE = "Add this note to the assignment"


def joined_ticks(page: str) -> dict[str, bool]:
    """Each joined note's tick on a page, by its name, with whether it is ticked."""
    return {name: bool(checked) for name, checked in re.findall(JOINED_TICK, page)}


def signed_joins(page: str) -> list[list[int]]:
    """The joined notes the page signed, each as its card, its place and whether ticked."""
    found = re.search(r'name="made_with" value="([^"]*)"', page)
    assert found is not None
    made = html.unescape(found.group(1)).rpartition(".")[0]
    signed: list[list[int]] = json.loads(made.partition(";")[2]).get("joined", [])
    return signed


def test_a_joined_note_has_a_tick_of_its_own_with_its_words_and_both_dates(
    tmp_path: pathlib.Path,
) -> None:
    """A note joined to its card shows on that card with its own tick, unselected, its
    words, and, where its deadline is not the card's due date, both dates with where each
    was read; the page signs the tick as shown. A joined note whose deadline is the card's,
    or that names none, shows no second date."""
    with client_in(tmp_path) as client:
        page = client.post("/parent/inbox/read", data={"text": DATED_APART}).text
        undated = client.post("/parent/inbox/read", data={"text": NOTED}).text

    shown = card(page, "Short essay")

    assert joined_ticks(shown) == {"join-1-0": False}
    assert (
        f'<span>{ADD_THIS_NOTE}<span class="visually-hidden"> for Short essay, Religion</span>'
    ) in shown
    assert (
        "The teacher's note for November 3 also says: "
        '<q class="authored-text">Short essay due Friday.</q>'
    ) in html.unescape(shown)
    assert (
        "The card says due Tuesday, November 3, 2026; the teacher's note for November 3 says "
        "Friday, November 6, 2026."
    ) in html.unescape(shown)
    assert signed_joins(page) == [[1, 0, 0]]
    assert joined_ticks(card(undated, "Short essay")) == {"join-1-0": False}
    assert "The card says" not in undated
    assert page.count(ADD_THIS_NOTE) == 1


@pytest.mark.parametrize(
    ("sent", "path", "status"),
    [
        ({"recheck": "1"}, "find", 200),
        ({"link-0-0": "1"}, "keep", 422),
        ({"find": "99"}, "find", 422),
        ({"recheck": "1", "made_with": "0.0"}, "find", 200),
        ({"made_with": "0.0"}, "keep", 409),
    ],
    ids=[
        "review-changes",
        "refused-save",
        "refused-press",
        "restart",
        "restart-saved",
    ],
)
def test_a_joined_notes_tick_comes_back_as_sent(
    tmp_path: pathlib.Path, sent: dict[str, str], path: str, status: int
) -> None:
    """A joined note's tick sent with Review changes, a refused save or press, or a page
    made before the application started again, writes nothing and comes back ticked,
    signed as the returned page shows it."""
    with client_in(tmp_path) as client:
        page = client.post("/parent/inbox/read", data={"text": DATED_APART}).text
        form = ticked_form(page) | {"join-1-0": "1"} | sent
        answer = client.post(f"/parent/inbox/{path}", data=form)
        rows = store_of(client).all_assignments()

    assert answer.status_code == status
    assert rows == []
    assert joined_ticks(card(answer.text, "Short essay")) == {"join-1-0": True}
    assert signed_joins(answer.text) == [[1, 0, 1]]


def teacher_note_claims(client: TestClient) -> dict[str, list[str]]:
    """The dates claimed in a teacher's note on record, by the title of their homework."""
    store = store_of(client)
    return {
        row.title: [
            each.asserted_value
            for each in store.deadline_records(row.assignment_id)
            if each.seen_in == "the teacher's note"
        ]
        for row in store.all_assignments()
    }


def test_a_joined_note_saves_its_date_in_one_press_once_selected(
    tmp_path: pathlib.Path,
) -> None:
    """An untouched Save writes the card with nothing of its joined note. Ticked and pressed
    with Save, the tick does what the page said it does if selected: the one press writes
    the note's date as a claim beside the card's own due date, which stays, and the same
    press sent again writes nothing more."""
    with client_in(tmp_path) as client:
        page = client.post("/parent/inbox/read", data={"text": DATED_APART}).text
        form = ticked_form(page) | {"join-1-0": "1"}
        changed = client.post("/parent/inbox/keep", data=form)
        due = {row.title: row.due_date for row in store_of(client).all_assignments()}
        claimed = teacher_note_claims(client)
        again = client.post("/parent/inbox/keep", data=form)
        claimed_again = teacher_note_claims(client)
    (tmp_path / "untouched").mkdir()
    with client_in(tmp_path / "untouched") as client:
        page = client.post("/parent/inbox/read", data={"text": DATED_APART}).text
        untouched = client.post("/parent/inbox/keep", data=ticked_form(page))
        left = teacher_note_claims(client)

    assert "If selected: New." in card(page, "Short essay")
    assert changed.status_code == 303
    assert claimed["Short essay"] == ["2026-11-06"]
    assert due["Short essay"] == datetime.date(2026, 11, 3)
    assert again.status_code == 303
    assert claimed_again == claimed
    assert untouched.status_code == 303
    assert left["Short essay"] == []


ALONE = NOTED.replace("Short essay Due.", "Short essay due Friday. Write it in ink.").replace(
    "Write 250 words on your question.\n", ""
)
"""The two days, with the note above the Tuesday's card naming Friday and saying more, and
the card giving no instruction of its own."""


def instruction_texts(client: TestClient) -> dict[str, list[str]]:
    """The school's instructions kept, by the title of their homework."""
    store = store_of(client)
    rows = {row.assignment_id: row.title for row in store.all_assignments()}
    readable = store.school_instruction_readings(list(rows)).readable
    return {
        rows[name]: [item.text for item in standing.kept] for name, standing in readable.items()
    }


def test_a_joined_notes_tick_saves_its_claim_and_words_in_one_press(
    tmp_path: pathlib.Path,
) -> None:
    """A joined note that says more, ticked and pressed with Save on the page that showed it
    unselected, writes in that one press its date as a claim and its words as the school's
    instruction; nothing comes back to be saved again."""
    with client_in(tmp_path) as client:
        page = client.post("/parent/inbox/read", data={"text": ALONE}).text
        saved = client.post("/parent/inbox/keep", data=ticked_form(page) | {"join-1-0": "1"})
        claimed = teacher_note_claims(client)
        kept = instruction_texts(client)

    assert joined_ticks(card(page, "Short essay")) == {"join-1-0": False}
    assert "If selected: " in card(page, "Short essay")
    assert saved.status_code == 303
    assert claimed["Short essay"] == ["2026-11-06"]
    assert kept["Short essay"] == ["Short essay due Friday. Write it in ink."]


def test_an_untick_of_a_selected_joined_note_saves_in_one_press_without_it(
    tmp_path: pathlib.Path,
) -> None:
    """A joined note shown selected, unticked and pressed with Save, saves the card in that
    one press as the page said it would be if not selected: nothing of the note is written."""
    with client_in(tmp_path) as client:
        page = client.post("/parent/inbox/read", data={"text": DATED_APART}).text
        form = ticked_form(page) | {"join-1-0": "1", "recheck": "1"}
        reviewed = client.post("/parent/inbox/find", data=form).text
        sent = ticked_form(reviewed)
        sent.pop("join-1-0")
        saved = client.post("/parent/inbox/keep", data=sent)
        rows = store_of(client).all_assignments()
        claimed = teacher_note_claims(client)

    assert joined_ticks(card(reviewed, "Short essay")) == {"join-1-0": True}
    assert "If not selected: New." in card(reviewed, "Short essay")
    assert saved.status_code == 303
    assert [row.title for row in rows if row.title == "Short essay"] == ["Short essay"]
    assert claimed["Short essay"] == []


REVIEW_NO_LONGER_MATCHES = (
    "This review does not match what saving would do, so nothing was saved. "
    "Check the review, then save."
)


def test_a_joined_tick_saved_against_a_record_moved_under_the_page_writes_nothing(
    tmp_path: pathlib.Path,
) -> None:
    """The card saved from another tab after the page was shown: a joined note ticked on
    the page and pressed with Save would add to a saved row, not make the new one the page
    said, so nothing is written and the review comes back saying it does not match what
    saving would do, without naming the selection as the cause, with the tick as sent."""
    with client_in(tmp_path) as client:
        page = client.post("/parent/inbox/read", data={"text": DATED_APART}).text
        assert client.post("/parent/inbox/keep", data=ticked_form(page)).status_code == 303
        before = record_counts(client)
        moved = client.post("/parent/inbox/keep", data=ticked_form(page) | {"join-1-0": "1"})
        after = record_counts(client)
        claimed = teacher_note_claims(client)

    assert moved.status_code == 409
    assert REVIEW_NO_LONGER_MATCHES in moved.text
    assert SELECTIONS_CHANGE not in moved.text
    assert after == before
    assert claimed["Short essay"] == []
    assert joined_ticks(card(moved.text, "Short essay")) == {"join-1-0": True}


CHOOSE_INSTRUCTIONS = "Choose which school instructions apply. Nothing was saved."


def test_a_joined_tick_whose_effect_asks_a_question_comes_back_asking_it(
    tmp_path: pathlib.Path,
) -> None:
    """A joined note whose words, once selected, make its card ask which instructions apply:
    the page says so before the tick, a tick pressed with Save comes back with that question
    and says to choose, writing nothing, and the answer saves."""
    with client_in(tmp_path) as client:
        page = client.post("/parent/inbox/read", data={"text": INKED}).text
        asked = client.post("/parent/inbox/keep", data=ticked_form(page) | {"join-1-0": "1"})
        rows = store_of(client).all_assignments()
        both = ticked_form(asked.text) | {"apply-1-0": "1", "apply-1-1": "1"}
        saved = client.post("/parent/inbox/keep", data=both)
        claimed = teacher_note_claims(client)

    assert "If selected: Needs your answer." in card(page, "Short essay")
    assert asked.status_code == 200
    assert CHOOSE_INSTRUCTIONS in asked.text
    assert SELECTIONS_CHANGE not in asked.text
    assert rows == []
    assert saved.status_code == 303
    assert claimed["Short essay"] == ["2026-11-06"]


INKED = NOTED.replace("Short essay Due.", "Short essay due Friday. Write it in ink.")
"""The two days, with the note above the Tuesday's card naming Friday and saying more."""


def record_counts(client: TestClient) -> tuple[int, int, int]:
    """How many rows, claims and school instructions the record keeps."""
    store = store_of(client)
    names = [row.assignment_id for row in store.all_assignments()]
    readable = store.school_instruction_readings(names).readable
    return (
        len(names),
        sum(len(store.deadline_records(name)) for name in names),
        sum(len(standing.kept) for standing in readable.values()),
    )


@pytest.mark.parametrize("untick", [False, True], ids=["kept", "unticked"])
def test_a_saved_joined_note_read_again_shows_selected_and_saving_writes_nothing_new(
    tmp_path: pathlib.Path, untick: bool
) -> None:
    """A joined note saved selected, its text pasted again: the note shows selected, signed
    so, and Save writes nothing new, ticked or with the tick taken off, which removes
    nothing."""
    with client_in(tmp_path) as client:
        page = client.post("/parent/inbox/read", data={"text": INKED}).text
        form = ticked_form(page) | {"join-1-0": "1"}
        reviewed = client.post("/parent/inbox/find", data=form | {"recheck": "1"}).text
        both = ticked_form(reviewed) | {"apply-1-0": "1", "apply-1-1": "1"}
        assert client.post("/parent/inbox/keep", data=both).status_code == 303
        before = record_counts(client)
        claimed = teacher_note_claims(client)
        again = client.post("/parent/inbox/read", data={"text": INKED}).text
        sent = ticked_form(again)
        if untick:
            sent.pop("join-1-0")
        saved = client.post("/parent/inbox/keep", data=sent)
        after = record_counts(client)

    assert claimed["Short essay"] == ["2026-11-06"]
    assert joined_ticks(card(again, "Short essay")) == {"join-1-0": True}
    assert signed_joins(again) == [[1, 0, 1]]
    assert "If not selected: " in card(again, "Short essay")
    assert saved.status_code == 303
    assert after == before


def test_the_page_that_reads_no_store_lists_a_joined_notes_tick(tmp_path: pathlib.Path) -> None:
    """With a claim on record that cannot be read, the page that reads no store says which
    joined note was selected to add to its card, for a save and for a press, and says
    nothing of one left unselected."""
    with client_in(tmp_path) as client:
        page = client.post("/parent/inbox/read", data={"text": DATED_APART}).text
        assert client.post("/parent/inbox/keep", data=ticked_form(page)).status_code == 303
        claims_damaged(client)
        form = ticked_form(page) | {"join-1-0": "1"}
        saved = client.post("/parent/inbox/keep", data=form)
        pressed = client.post("/parent/inbox/find", data=form | {"recheck": "1"})
        untouched = client.post("/parent/inbox/keep", data=ticked_form(page))

    for answer in (saved, pressed):
        assert answer.status_code == 500
        assert (
            "Card 1: no answer about the assignment. Teacher's note 1 on this card "
            "selected to add to the assignment."
        ) in answer.text
    assert untouched.status_code == 500
    assert "Card 1:" not in untouched.text


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
    """On the page Review changes returns once the family selects the homework notes, a note
    shown unticked says what selecting it does. Ticked there and saved, it is written with
    the rest in one press."""
    with client_in(tmp_path) as client:
        page = homework_selected(client, NOTED)
        kept = client.post("/parent/inbox/keep", data=ticked_form(page) | {"take-5": "1"})
        rows = {row.title for row in store_of(client).all_assignments()}

    assert '<p class="helper">If selected: New.</p>' in card(page, "Quiz on unit two")
    assert page.count("If selected: ") == 3
    assert card(page, "Short essay").count("If selected: ") == 1
    assert "If selected" not in card(page, "Reading log")
    assert kept.status_code == 303
    assert kept.headers["location"].endswith("added=6&updated=0&unchanged=0")
    assert "Quiz on unit two" in rows
    assert len(rows) == 6


def test_a_note_unticked_by_the_family_is_not_written_in_one_press(tmp_path: pathlib.Path) -> None:
    """A homework note unticked on the page Review changes returns once the family selects
    the homework notes is left out of the save, which writes the rest in one press."""
    with client_in(tmp_path) as client:
        page = homework_selected(client, NOTED)
        form = ticked_form(page)
        del form["take-4"]
        kept = client.post("/parent/inbox/keep", data=form)
        rows = {row.title for row in store_of(client).all_assignments()}

    assert kept.status_code == 303
    assert kept.headers["location"].endswith("added=4&updated=0&unchanged=0")
    assert "Reading log" not in rows
    assert len(rows) == 4


def test_a_tick_on_a_form_the_page_did_not_sign_writes_nothing(tmp_path: pathlib.Path) -> None:
    """The page Review changes returns once the family selects the homework notes, sent
    without its signature and with one more note ticked, writes nothing: the review comes
    back with the ticks as they were sent."""
    with client_in(tmp_path) as client:
        page = homework_selected(client, NOTED)
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
    """The review of a paste with notes, as Review changes returns it once the family selects
    the homework notes, offers Review changes beside Save, with a line that says what it
    does. Pressed again with one more note ticked and one unticked, it shows the review again
    with the ticks as sent and writes nothing; the page it returns saves in one press. A
    paste with no note is not offered it."""
    with client_in(tmp_path) as client:
        plain = client.post("/parent/inbox/read", data={"text": NO_NOTE}).text
        page = homework_selected(client, NOTED)
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
    assert SAVE_SELECTED in again.text
    assert kept.status_code == 303
    assert kept.headers["location"].endswith("added=5&updated=0&unchanged=0")
    assert "Quiz on unit two" in rows
    assert "Reading log" not in rows


def test_a_paste_with_nothing_selected_offers_review_changes(tmp_path: pathlib.Path) -> None:
    """With no reading selected the page offers Review changes: a note ticked and reviewed
    comes back selected, with the save offered."""
    with client_in(tmp_path) as client:
        page = client.post("/parent/inbox/read", data={"text": ONE_UNCLEAR_NOTE}).text
        form = ticked_form(page) | {"take-0": "1", "recheck": "1"}
        again = client.post("/parent/inbox/find", data=form)
        rows = store_of(client).all_assignments()

    assert page.count(REVIEW_CHANGES) == 1
    assert again.status_code == 200
    assert rows == []
    assert numbers(again.text, TICKED) == {0}
    assert SAVE_SELECTED in again.text


def test_a_selection_that_changes_another_card_saves_nothing_and_says_why(
    tmp_path: pathlib.Path,
) -> None:
    """Two notes name the same work; with both selected by Review changes, the later one asks
    whether it is the same assignment. With the earlier one unticked the later one would be
    saved with no question, which its card did not say: nothing is saved, and the page says
    why, first and with the focus, with the ticks as sent."""
    with client_in(tmp_path) as client:
        page = homework_selected(client, TWICE_NOTED)
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
        f'{SELECTIONS_CHANGE} <a href="#card-1">Check this assignment</a></p>'
    ) in kept.text
    linked = re.findall(r'<article class="assignment reading-[a-z_]+" id="(card-\d+)"', kept.text)
    assert linked == ["card-1"]
    assert kept.text.count('id="card-') == 1
    assert 'id="card-' not in page
    assert numbers(kept.text, TICKED) == {1}


def test_the_same_review_saved_twice_writes_once(tmp_path: pathlib.Path) -> None:
    """The form of the page Review changes returns once the family selects the homework
    notes, sent twice, as a double press sends it: the second save finds each note in the row
    the page named, writes nothing, and says the five are unchanged."""
    with client_in(tmp_path) as client:
        page = homework_selected(client, NOTED)
        first = client.post("/parent/inbox/keep", data=ticked_form(page))
        second = client.post("/parent/inbox/keep", data=ticked_form(page))
        rows = store_of(client).all_assignments()

    assert first.status_code == second.status_code == 303
    assert second.headers["location"].endswith("added=0&updated=0&unchanged=5")
    assert len(rows) == 5


def test_a_note_card_answered_on_the_page_saves_in_one_press(tmp_path: pathlib.Path) -> None:
    """With both notes selected by Review changes, a note's card that asks whether it is the
    same assignment or new work, answered on that page, is saved with the answer in one
    press."""
    with client_in(tmp_path) as client:
        page = homework_selected(client, TWICE_NOTED)
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
    """From the page Review changes returns once the family selects the homework notes, a
    save or a press that is refused, or a save handed back for a question, writes nothing
    and shows the review again with each note ticked or not as the form sent it."""
    with client_in(tmp_path) as client:
        page = homework_selected(client, text)
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
    """With both notes selected by Review changes, a note answered as the same assignment is
    folded into the card for it and is not shown; the page sends its tick back with its
    answer. The card it is folded into asks which of the two notes' words apply, and with
    that answered the page saves in one press, as one row."""
    with client_in(tmp_path) as client:
        page = homework_selected(client, TWICE_NOTED)
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
    """With the homework notes selected by Review changes and saved, and a claim on record
    that cannot be read, the page that reads no store says of each note whether it was
    selected to save: as the form sent it for a save and for a press, and, for a first
    reading, not selected, as every note starts."""
    with client_in(tmp_path) as client:
        page = homework_selected(client, NOTED)
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
    assert "Card 4: no answer about the assignment; not selected." in first.text
    assert "Card 5: no answer about the assignment; not selected." in first.text
    assert plain.status_code == 200


UNDATED_AGAIN = (
    "Homework for Wren\n11/02/2026 - Monday\n"
    "Spanish - Assigned: Oral quiz: (Due:11/10/2026)\n"
    "11/03/2026 - Tuesday\nSpanish -\nOral quiz Due.\n"
)
"""A card on Monday, and on Tuesday a note that names it and settles no date."""

OTHER_DATE = (
    "Homework for Wren\n11/02/2026 - Monday\n"
    "Spanish - Assigned: Oral quiz: (Due:11/10/2026)\n"
    "11/03/2026 - Tuesday\nSpanish -\nOral quiz due Friday\n"
)
"""A card on Monday due the week after, and on Tuesday a note that names it and settles
another date, that Friday."""


def test_a_notes_question_offers_the_same_assignment_in_words_true_of_a_note(
    tmp_path: pathlib.Path,
) -> None:
    """With the notes selected by Review changes, a note's card that asks which it is says
    what each choice does to the due date. A note that settles no date, or the date beside
    it, offers the same assignment with that date kept; a note that settles another date
    offers it kept and offers it changed, each with the dates named. Nothing is chosen from
    the start, and the sentence above says no date changed."""
    with client_in(tmp_path) as client:
        same = homework_selected(client, TWICE_NOTED)
        undated = homework_selected(client, UNDATED_AGAIN)
        other = homework_selected(client, OTHER_DATE)

    radio = '<label><input type="radio" name="occurrence-1" value="{}"> {}</label>'
    kept = "The same assignment; keep its due date of {}"
    assert radio.format("keep", kept.format("Friday, November 6, 2026")) in same
    assert radio.format("keep", kept.format("Tuesday, November 10, 2026")) in undated
    assert radio.format("keep", kept.format("Tuesday, November 10, 2026")) in other
    assert (
        radio.format(
            "update",
            "The same assignment; change its due date from Tuesday, November 10, 2026 "
            "to Friday, November 6, 2026",
        )
        in other
    )
    for page in (same, undated):
        assert 'name="occurrence-1" value="update"' not in page
    for page in (same, undated, other):
        assert page.count('name="occurrence-1" value="keep"') == 1
        assert radio.format("new", "New work under the same name") in page
        assert "its due date changed" not in page
        assert "Say whether this is the same assignment or new work under the same name." in page
        assert not re.search(r'name="occurrence-1" value="[a-z]+" checked', page)


def test_a_note_said_to_be_the_card_with_its_date_kept_is_saved_with_the_cards_date(
    tmp_path: pathlib.Path,
) -> None:
    """With the note selected by Review changes, the choice to keep the due date travels
    with the page as its own answer through a review handed back, and the save writes one
    row with the card's due date. The choice to change it writes the note's date."""
    with client_in(tmp_path) as client:
        page = homework_selected(client, OTHER_DATE)
        form = ticked_form(page) | {"occurrence-1": "keep", "recheck": "1"}
        again = client.post("/parent/inbox/find", data=form)
        kept = client.post("/parent/inbox/keep", data=ticked_form(again.text))
        rows = store_of(client).all_assignments()
    with client_in(tmp_path / "changed") as client:
        page = homework_selected(client, OTHER_DATE)
        changed = client.post(
            "/parent/inbox/keep", data=ticked_form(page) | {"occurrence-1": "update"}
        )
        moved = store_of(client).all_assignments()

    assert '<input type="hidden" name="occurrence-1" value="keep">' in again.text
    assert '<input type="hidden" name="folded-1" value="0">' in again.text
    assert kept.status_code == changed.status_code == 303
    assert [(row.title, row.due_date) for row in rows] == [
        ("Oral quiz", datetime.date(2026, 11, 10))
    ]
    assert [(row.title, row.due_date) for row in moved] == [
        ("Oral quiz", datetime.date(2026, 11, 6))
    ]


def test_the_page_says_none_is_selected_only_when_none_is(tmp_path: pathlib.Path) -> None:
    """The sentence that no assignment is selected is on the page when no reading is
    selected and on no other page: a first reading whose one note is unselected, that note
    ticked on Review changes, two notes selected by Review changes, and those two unticked on
    another Review changes. The save on a review with notes names no count and carries
    nothing for the page script to rewrite; a review with no note keeps its counted save."""
    with client_in(tmp_path) as client:
        none = client.post("/parent/inbox/read", data={"text": ONE_UNCLEAR_NOTE}).text
        one = client.post(
            "/parent/inbox/find", data=ticked_form(none) | {"take-0": "1", "recheck": "1"}
        ).text
        both = homework_selected(client, TWICE_NOTED)
        form = ticked_form(both) | {"recheck": "1"}
        del form["take-0"], form["take-1"]
        neither = client.post("/parent/inbox/find", data=form).text
        plain = client.post("/parent/inbox/read", data={"text": NO_NOTE}).text

    for page in (none, neither):
        assert page.count(f'<p class="empty">{NOTHING_SELECTED}</p>') == 1
        assert page.count(SAVE_SELECTED) == 1
    for page in (one, both):
        assert NOTHING_SELECTED not in page
        assert page.count(SAVE_SELECTED) == 1
        assert "data-changed-label" not in page
        assert "data-unchanged-label" not in page
    assert NOTHING_SELECTED not in plain
    assert "Save selected assignments" not in plain
    assert ">Save 3 assignments</button>" in plain


OTHER_READING_LOG = "Homework for Wren\n11/02/2026 - Monday\nScience - Due: Reading log:\n"
"""A card of the class and title one of the notes names, from another text."""

NO_LONGER_MATCHES = (
    "This review does not match what saving would do, so nothing was saved. "
    "Check the review, then save."
)
COULD_NOT_BE_VERIFIED = (
    "This review could not be verified, so nothing was saved. Check it again, then save."
)
SUMMARY = '<p class="problem" role="alert" id="problem-summary" tabindex="-1" autofocus>'


@pytest.mark.parametrize(
    ("sent", "ticked"),
    [({}, {2, 3, 4}), ({"take-5": "1"}, {2, 3, 4, 5})],
    ids=["record-only", "record-and-selection"],
)
def test_a_review_the_record_moved_under_is_refused_without_naming_the_selections(
    tmp_path: pathlib.Path, sent: dict[str, str], ticked: set[int]
) -> None:
    """Another save, between the page Review changes returns once the family selects the
    homework notes and the save of that page, puts a row where a note said it would make
    one. Whether or not a tick changed as well, nothing of the review is saved, the page says
    so without laying it to the selections, links to the one note it is about, and keeps the
    ticks as sent."""
    with client_in(tmp_path) as client:
        page = homework_selected(client, NOTED)
        other = client.post("/parent/inbox/keep", data={"text": OTHER_READING_LOG})
        answer = client.post("/parent/inbox/keep", data=ticked_form(page) | sent)
        rows = [row.title for row in store_of(client).all_assignments()]

    assert other.status_code == 303
    assert answer.status_code == 409
    assert rows == ["Reading log"]
    assert (
        f'{SUMMARY}{NO_LONGER_MATCHES} <a href="#card-4">Check this assignment</a></p>'
    ) in answer.text
    assert "Your selections" not in answer.text
    assert "changed since this review" not in answer.text
    assert answer.text.count('id="card-') == 1
    assert numbers(answer.text, TICKED) == ticked


def test_a_review_open_across_a_restart_is_checked_again_and_then_saves(
    tmp_path: pathlib.Path,
) -> None:
    """The page Review changes returns once the family selects the homework notes, made by
    one run of the application and saved to the next with one more note ticked and one
    unticked, cannot be verified: nothing is saved, the page says so without saying any
    homework changed, and it comes back with the ticks as sent and no link to a card. The
    page handed back saves in one press."""
    with client_in(tmp_path) as client:
        page = homework_selected(client, NOTED)
    form = ticked_form(page) | {"take-5": "1"}
    del form["take-4"]
    with client_in(tmp_path) as client:
        refused = client.post("/parent/inbox/keep", data=form)
        unwritten = store_of(client).all_assignments()
        kept = client.post("/parent/inbox/keep", data=ticked_form(refused.text))
        rows = {row.title for row in store_of(client).all_assignments()}

    assert refused.status_code == 409
    assert unwritten == []
    assert f"{SUMMARY}{COULD_NOT_BE_VERIFIED}</p>" in refused.text
    assert "changed since this review" not in refused.text
    assert "Your selections" not in refused.text
    assert 'id="card-' not in refused.text
    assert numbers(refused.text, TICKED) == {2, 3, 5}
    assert kept.status_code == 303
    assert kept.headers["location"].endswith("added=5&updated=0&unchanged=0")
    assert "Quiz on unit two" in rows
    assert "Reading log" not in rows


NO_DATE_BESIDE = (
    "Homework for Wren\n11/02/2026 - Monday\nArt -\nPoster Due.\n\n"
    "11/04/2026 - Wednesday\nArt -\nPoster due Friday\n"
)
"""Two notes of one class on two days that name the same work: the first settles no date."""


def test_a_notes_question_with_no_date_beside_it_names_no_date(tmp_path: pathlib.Path) -> None:
    """With both notes selected by Review changes, when the first reading of the name has no
    due date there is none to keep or change: the one choice for the same assignment names
    no date, and nothing is chosen."""
    with client_in(tmp_path) as client:
        page = homework_selected(client, NO_DATE_BESIDE)

    assert (
        '<label><input type="radio" name="occurrence-1" value="update"> The same assignment</label>'
    ) in page
    assert 'name="occurrence-1" value="keep"' not in page
    assert "its due date" not in page
    assert not re.search(r'name="occurrence-1" value="[a-z]+" checked', page)


FIELDS = """Homework for Wren
11/02/2026 - Monday
Humanities -
Study for the vocabulary quiz on Thursday. Map project due on Wednesday.

08 Geometry -
Quiz on unit two

11/03/2026 - Tuesday
Science -
Online practice due Tuesday
"""
"""A dated note, a note that names a date, a note that may be homework, and a note due on a
weekday it was first listed on, so either Tuesday may be meant."""

EDITS_IN_REVIEW = (
    "Your changes to a title or due date were not reviewed, so nothing was saved. "
    "Check them, then press Review changes."
)


def key_of(page: str, title: str) -> int:
    """The key of the note card with this title, as its offered field names it."""
    found = re.search(r'name="offered-(\d+)"', card(page, title))
    assert found is not None, title
    return int(found.group(1))


def when_options(body: str, key: int) -> list[tuple[str, bool]]:
    """Each due-date choice of a card, in order, with whether it is chosen."""
    return [
        (value, bool(checked))
        for value, checked in re.findall(
            rf'<input type="radio" name="when-{key}" value="([^"]+)"( checked)?>', body
        )
    ]


def signed_edits(page: str) -> dict[int, list[str]]:
    """The title and due date the page signed for each note card."""
    found = re.search(r'name="made_with" value="([^"]*)"', page)
    assert found is not None
    made = html.unescape(found.group(1)).rpartition(".")[0]
    context = json.loads(made.partition(";")[2])
    return {int(entry[0]): [str(entry[1]), str(entry[2])] for entry in context["edits"]}


DUE_CHOICES = {
    "Map project": [("2026-11-04", True), ("none", False), ("other", False)],
    "Study for the vocabulary quiz on Thursday": [
        ("2026-11-05", False),
        ("none", True),
        ("other", False),
    ],
    "Quiz on unit two": [("none", True), ("other", False)],
    "Online practice": [
        ("2026-11-03", False),
        ("2026-11-10", False),
        ("none", True),
        ("other", False),
    ],
}
"""The due-date choices of each note card on the first view: the date as read, chosen; each
date a weekday may mean; each date the note names; no due date, chosen when none was read;
and another date."""


def details_opened(client: TestClient, page: str, *titles: str) -> str:
    """The review with the details of the note cards with these titles opened, one Edit
    details press after another, each from the page the one before returned."""
    for title in titles:
        answer = opened(client, page, key_of(page, title))
        assert answer.status_code == 200
        page = answer.text
    return page


def test_each_note_card_has_a_title_box_and_one_due_date_choice(tmp_path: pathlib.Path) -> None:
    """Each note card, its details opened, has a box with its whole title and one group of
    due-date choices, with the date as read chosen, or no due date when none was read; each
    control names its card in words a screen reader says. A card read from no note has
    neither."""
    with client_in(tmp_path) as client:
        first = client.post("/parent/inbox/read", data={"text": FIELDS}).text
        page = details_opened(client, first, *DUE_CHOICES)
        noted_first = client.post("/parent/inbox/read", data={"text": NOTED}).text
        noted = details_opened(client, noted_first, "Reading log")

    for title, choices in DUE_CHOICES.items():
        body = card(page, title)
        key = key_of(page, title)
        assert f'<label>Title<span class="visually-hidden"> for {title}, ' in body
        assert f'<input type="text" name="title-{key}" value="{title}"' in body
        assert when_options(body, key) == choices
        assert f'<legend>Due date<span class="visually-hidden"> for {title}, ' in body
        assert f'<input type="date" name="when_other-{key}" value=""' in body
        assert f'<span class="visually-hidden"> Another date for {title}, ' in body
    assert "Wednesday, November 4, 2026, as the note says" in card(page, "Map project")
    assert "Thursday, November 5, 2026, named in the note" in card(
        page, "Study for the vocabulary quiz on Thursday"
    )
    assert ">Tuesday, November 10, 2026<" in card(page, "Online practice")
    assert 'type="text" name="title-' in card(noted, "Reading log")
    for title in ("Textbook page 12", "Short essay"):
        assert 'name="title-' not in card(noted, title)
        assert 'name="when-' not in card(noted, title)


def test_the_page_signs_the_title_and_due_date_each_note_card_shows(
    tmp_path: pathlib.Path,
) -> None:
    """The form's signature carries, for each note card, the title and due date it shows."""
    with client_in(tmp_path) as client:
        page = client.post("/parent/inbox/read", data={"text": FIELDS}).text

    assert signed_edits(page) == {
        key_of(page, "Study for the vocabulary quiz on Thursday"): [
            "Study for the vocabulary quiz on Thursday",
            "",
        ],
        key_of(page, "Map project"): ["Map project", "2026-11-04"],
        key_of(page, "Quiz on unit two"): ["Quiz on unit two", ""],
        key_of(page, "Online practice"): ["Online practice", ""],
    }


def test_an_edit_pressed_with_save_writes_nothing_and_comes_back_as_typed(
    tmp_path: pathlib.Path,
) -> None:
    """On the page Review changes returns once the family selects the homework notes, with a
    card's details opened, a title changed and Save sent by hand: nothing is written, the
    review comes back with the title applied and the ticks, titles and dates as sent, and the
    summary, which takes the focus, says so and links to the one card changed. Two cards
    changed: no link."""
    with client_in(tmp_path) as client:
        page = details_opened(client, homework_selected(client, FIELDS), "Map project")
        mapped, quiz = key_of(page, "Map project"), key_of(page, "Quiz on unit two")
        practice = key_of(page, "Online practice")
        form = ticked_form(page) | {f"title-{mapped}": "Map of the river", f"take-{quiz}": "1"}
        one = client.post("/parent/inbox/keep", data=form)
        two = client.post("/parent/inbox/keep", data=form | {f"when-{practice}": "2026-11-10"})
        rows = store_of(client).all_assignments()

    assert (one.status_code, two.status_code) == (200, 200)
    assert rows == []
    assert (
        f'{SUMMARY}{EDITS_IN_REVIEW} <a href="#card-{mapped}">Check this assignment</a></p>'
    ) in one.text
    body = card(one.text, "Map of the river")
    assert f'id="card-{mapped}" tabindex="-1"' in body
    assert f'<input type="text" name="title-{mapped}" value="Map of the river"' in body
    assert when_options(body, mapped) == DUE_CHOICES["Map project"]
    assert quiz in numbers(one.text, TICKED)
    assert f"{SUMMARY}{EDITS_IN_REVIEW}</p>" in two.text
    assert 'id="card-' not in two.text
    assert ("2026-11-10", True) in when_options(card(two.text, "Online practice"), practice)


def test_review_changes_then_save_writes_the_corrected_title_and_the_familys_date(
    tmp_path: pathlib.Path,
) -> None:
    """From the page Review changes returns once the family selects the homework notes,
    Review changes pressed with edits shows them applied and writes nothing; Save on the page
    it returns writes the corrected title, with the school's date kept, and the chosen
    Tuesday as the family's entry. The same text read again shows both notes already saved,
    under their saved titles and dates, and asks nothing."""
    with client_in(tmp_path) as client:
        page = homework_selected(client, FIELDS)
        mapped, practice = key_of(page, "Map project"), key_of(page, "Online practice")
        form = ticked_form(page) | {
            f"title-{mapped}": "  Map of the   river ",
            f"when-{practice}": "2026-11-10",
            "recheck": "1",
        }
        reviewed = client.post("/parent/inbox/find", data=form)
        unwritten = store_of(client).all_assignments()
        kept = client.post("/parent/inbox/keep", data=ticked_form(reviewed.text))
        rows = {row.title: row for row in store_of(client).all_assignments()}
        again = client.post("/parent/inbox/read", data={"text": FIELDS}).text

    assert (reviewed.status_code, unwritten) == (200, [])
    assert signed_edits(reviewed.text)[mapped] == ["Map of the river", "2026-11-04"]
    assert signed_edits(reviewed.text)[practice] == ["Online practice", "2026-11-10"]
    assert "Due Tuesday, November 10, 2026" in card(reviewed.text, "Online practice")
    assert kept.status_code == 303
    assert "Map project" not in rows
    river, online = rows["Map of the river"], rows["Online practice"]
    assert (river.due_date, river.origins["due_date"]) == (
        datetime.date(2026, 11, 4),
        SourceChannel.LMS,
    )
    assert (online.due_date, online.origins["due_date"]) == (
        datetime.date(2026, 11, 10),
        SourceChannel.PARENT_ENTRY,
    )
    for title in ("Map project", "Online practice"):
        assert '<span class="pill">Already saved</span>' in card(again, title)
    assert (
        'It\'s saved here as <span class="authored-text">Map of the river</span>, Humanities, '
        "due Wednesday, November 4, 2026."
    ) in card(again, "Map project")
    assert "Due Tuesday, November 10, 2026" in card(again, "Online practice")
    for asked in ("identity-question-", "occurrence-question-", "instructions-question-"):
        assert asked not in again


def test_a_cleared_date_saves_no_due_date_and_no_school_claim(tmp_path: pathlib.Path) -> None:
    """No due date chosen for a dated note on the page Review changes returns once the family
    selects the homework notes, reviewed and saved: the row has no due date, no origin for
    one, and no claim of the note's date."""
    with client_in(tmp_path) as client:
        page = homework_selected(client, FIELDS)
        mapped = key_of(page, "Map project")
        form = ticked_form(page) | {f"when-{mapped}": "none", "recheck": "1"}
        reviewed = client.post("/parent/inbox/find", data=form)
        kept = client.post("/parent/inbox/keep", data=ticked_form(reviewed.text))
        store = store_of(client)
        row = next(row for row in store.all_assignments() if row.title == "Map project")
        claims = store.claim_history(row.assignment_id)

    body = card(reviewed.text, "Map project")
    assert reviewed_line("Map project", "No due date yet") in body
    assert f'<input type="hidden" name="when-{mapped}" value="none">' in body
    assert kept.status_code == 303
    assert row.due_date is None
    assert "due_date" not in row.origins
    assert claims == []


def test_an_untouched_review_with_note_fields_saves_in_one_press(tmp_path: pathlib.Path) -> None:
    """The page Review changes returns once the family selects the homework notes, sent back
    as shown, with no title or date field since no details were opened, saves in one press,
    each note with its title and date as read."""
    with client_in(tmp_path) as client:
        page = homework_selected(client, FIELDS)
        form = ticked_form(page)
        kept = client.post("/parent/inbox/keep", data=form)
        rows = {row.title: row for row in store_of(client).all_assignments()}

    assert not any(name.startswith("title-") for name in form)
    assert not any(name.startswith("when-") for name in form)
    assert kept.status_code == 303
    assert kept.headers["location"].endswith("added=3&updated=0&unchanged=0")
    assert rows["Map project"].due_date == datetime.date(2026, 11, 4)
    assert rows["Online practice"].due_date is None


NOT_A_DUE_DATE = "Check the due date. Enter it as year, month, and day."
FAR_DUE_DATE = "Check the due-date year. Enter a date within one year of today."
LONG_TITLE = "A title is at most 200 characters."


@pytest.mark.parametrize("path", ["keep", "find"])
@pytest.mark.parametrize(
    ("typed", "said"),
    [("", NOT_A_DUE_DATE), ("soon", NOT_A_DUE_DATE), ("2028-11-04", FAR_DUE_DATE)],
    ids=["empty", "not-a-date", "out-of-range"],
)
def test_another_date_that_cannot_be_read_is_refused_with_the_typing_kept(
    tmp_path: pathlib.Path, path: str, typed: str, said: str
) -> None:
    """On the page Review changes returns once the family selects the homework notes, another
    date chosen on a selected note with an empty box, a value that is no date, or a date
    outside the school year, pressed with Save or Review changes: refused with the page's
    sentence for that date, nothing written, the summary taking the focus and linking to the
    card's due-date choices, and every tick, title and date as typed."""
    with client_in(tmp_path) as client:
        page = homework_selected(client, FIELDS)
        mapped, quiz = key_of(page, "Map project"), key_of(page, "Quiz on unit two")
        study = key_of(page, "Study for the vocabulary quiz on Thursday")
        practice = key_of(page, "Online practice")
        form = ticked_form(page) | {
            f"when-{mapped}": "other",
            f"when_other-{mapped}": typed,
            f"title-{study}": "Vocabulary quiz",
            f"when-{practice}": "2026-11-10",
            f"take-{quiz}": "1",
        }
        if path == "find":
            form["recheck"] = "1"
        answer = client.post(f"/parent/inbox/{path}", data=form)
        rows = store_of(client).all_assignments()

    assert answer.status_code == 422
    assert rows == []
    assert (
        f'{SUMMARY}{said} <a href="#when-{mapped}">Check this assignment</a></p>'
    ) in answer.text
    body = card(answer.text, "Map project")
    assert f'id="when-{mapped}" tabindex="-1" aria-describedby="problem-summary"' in body
    assert ("other", True) in when_options(body, mapped)
    assert f'<input type="date" name="when_other-{mapped}" value="{typed}"' in body
    assert f'<input type="text" name="title-{study}" value="Vocabulary quiz"' in answer.text
    assert ("2026-11-10", True) in when_options(card(answer.text, "Online practice"), practice)
    assert quiz in numbers(answer.text, TICKED)
    assert 'id="card-' not in answer.text


@pytest.mark.parametrize("path", ["keep", "find"])
def test_a_title_longer_than_its_box_allows_is_refused_with_the_typing_kept(
    tmp_path: pathlib.Path, path: str
) -> None:
    """A title longer than the box allows, typed on the page Review changes returns once the
    family selects the homework notes and sent by a form the page did not make, is refused
    with the sentence the entry form uses for it, nothing written, the summary linking to
    the title box, and every tick, title and date as typed."""
    long = "x" * 201
    with client_in(tmp_path) as client:
        page = homework_selected(client, FIELDS)
        mapped, practice = key_of(page, "Map project"), key_of(page, "Online practice")
        form = ticked_form(page) | {f"title-{mapped}": long, f"when-{practice}": "2026-11-10"}
        if path == "find":
            form["recheck"] = "1"
        answer = client.post(f"/parent/inbox/{path}", data=form)
        rows = store_of(client).all_assignments()

    assert answer.status_code == 422
    assert rows == []
    assert (
        f'{SUMMARY}{LONG_TITLE} <a href="#title-{mapped}">Check this assignment</a></p>'
    ) in answer.text
    assert f'<input type="text" name="title-{mapped}" value="{long}"' in answer.text
    assert f'id="title-{mapped}" aria-describedby="problem-summary"' in answer.text
    assert ("2026-11-10", True) in when_options(card(answer.text, "Online practice"), practice)


def test_an_edit_on_a_note_not_selected_does_not_hold_the_save_back(
    tmp_path: pathlib.Path,
) -> None:
    """On the page Review changes returns once the family selects the homework notes, an edit
    on a note left out of the save writes nothing, so Save goes ahead for the selected notes
    in one press. The edit, an unreadable date among them, is kept as typed on the page a
    second Review changes returns, carried by the card shown as reviewed, and nothing is
    refused."""
    with client_in(tmp_path) as client:
        page = details_opened(client, homework_selected(client, FIELDS), "Quiz on unit two")
        quiz = key_of(page, "Quiz on unit two")
        form = ticked_form(page) | {
            f"title-{quiz}": "Unit two quiz",
            f"when-{quiz}": "other",
            f"when_other-{quiz}": "soon",
        }
        reviewed = client.post("/parent/inbox/find", data=form | {"recheck": "1"})
        kept = client.post("/parent/inbox/keep", data=form)
        rows = {row.title for row in store_of(client).all_assignments()}

    assert reviewed.status_code == 200
    assert 'id="problem-summary"' not in reviewed.text
    body = card(reviewed.text, "Unit two quiz")
    assert f'<input type="hidden" name="title-{quiz}" value="Unit two quiz">' in body
    assert f'<input type="hidden" name="when_other-{quiz}" value="soon">' in body
    assert f'<input type="hidden" name="when-{quiz}" value="other">' in body
    assert quiz not in numbers(reviewed.text, TICKED)
    assert kept.status_code == 303
    assert kept.headers["location"].endswith("added=3&updated=0&unchanged=0")
    assert rows == {"Map project", "Study for the vocabulary quiz on Thursday", "Online practice"}


NOTED_EDITS = {
    "Map project": ("Map of the river", "other", "2026-11-06"),
    "Reading log": ("Reading journal", "none", ""),
}
"""A title and another date typed on a selected note of ``NOTED``, and a title and no due date
on one left out."""


@pytest.mark.parametrize(
    ("text", "before", "typing", "sent", "path", "status", "said"),
    [
        (
            NOTED,
            None,
            NOTED_EDITS,
            {"take-5": "1", "take-4": None, "link-0-0": "1"},
            "keep",
            422,
            "The choices to link her notes could not be read",
        ),
        (
            NOTED,
            None,
            NOTED_EDITS,
            {"take-5": "1", "take-4": None, "made_with": "0.0"},
            "keep",
            409,
            COULD_NOT_BE_VERIFIED,
        ),
        (
            NOTED,
            None,
            NOTED_EDITS,
            {"take-5": "1", "take-4": None, "find": "99"},
            "find",
            422,
            "That press isn&#39;t one this review offered",
        ),
        (
            NOTED,
            None,
            NOTED_EDITS,
            {"take-5": "1", "take-4": None, "search-3": "map", "find": "3"},
            "find",
            200,
            'id="search-hint-3"',
        ),
        (
            WITH_A_QUESTION,
            None,
            {"Quiz on unit two": ("Unit two quiz", "other", "2026-11-09")},
            {},
            "keep",
            200,
            "A question above still needs your answer.",
        ),
        (
            TWICE_NOTED,
            None,
            {"Reading log": ("Reading journal", "none", "")},
            {"take-0": None},
            "keep",
            200,
            SELECTIONS_CHANGE,
        ),
        (
            NOTED,
            OTHER_READING_LOG,
            {"Quiz on unit two": ("Unit two quiz", "other", "2026-11-09")},
            {},
            "keep",
            409,
            NO_LONGER_MATCHES,
        ),
    ],
    ids=[
        "unreadable-links",
        "unsigned",
        "refused-press",
        "search",
        "open-question",
        "selections-change",
        "record-moved",
    ],
)
def test_a_page_handed_back_carries_the_titles_and_dates_as_typed(
    tmp_path: pathlib.Path,
    text: str,
    before: str | None,
    typing: dict[str, tuple[str, str, str]],
    sent: dict[str, str | None],
    path: str,
    status: int,
    said: str,
) -> None:
    """From the page Review changes returns once the family selects the homework notes, a
    refused form, a search press, a question left open, and each changed-review refusal
    write nothing of the text and show the review again with every tick, title and date as
    the form sent them, on notes selected and notes left out."""
    with client_in(tmp_path) as client:
        page = homework_selected(client, text)
        form = ticked_form(page)
        typed = {}
        for title, (new_title, when, other) in typing.items():
            key = min(
                int(number)
                for number in re.findall(
                    rf'<h2>{re.escape(title)}</h2>(?:(?!</article>).)*?name="offered-(\d+)"',
                    page,
                    re.S,
                )
            )
            typed[key] = (new_title, when, other)
            # Typed in the card's details, opened by its Edit details press.
            form |= {
                f"title-{key}": new_title,
                f"when-{key}": when,
                f"when_other-{key}": other,
                f"editing-{key}": "1",
            }
        for name, value in sent.items():
            if value is None:
                del form[name]
            else:
                form[name] = value
        if before is not None:
            assert client.post("/parent/inbox/keep", data={"text": before}).status_code == 303
        answer = client.post(f"/parent/inbox/{path}", data=form)
        rows = store_of(client).all_assignments()

    assert answer.status_code == status
    assert len(rows) == (0 if before is None else 1)
    assert said in answer.text
    for key, (new_title, when, other) in typed.items():
        assert f'<input type="text" name="title-{key}" value="{new_title}"' in answer.text
        assert (when, True) in when_options(answer.text, key)
        assert f'<input type="date" name="when_other-{key}" value="{other}"' in answer.text
    assert numbers(answer.text, TICKED) == {
        int(name.removeprefix("take-")) for name in form if name.startswith("take-")
    }


TYPING_LOST = (
    '<p class="helper" id="typing-lost">'
    "Selections, titles, and due dates aren't kept when the text is edited.</p>"
)


def test_edit_the_text_says_the_selections_titles_and_dates_are_not_kept(
    tmp_path: pathlib.Path,
) -> None:
    """Beside Edit the text on a review with notes, one line says the selections, titles and
    due dates are not kept, and the press names it. A review with no note has neither."""
    with client_in(tmp_path) as client:
        page = client.post("/parent/inbox/read", data={"text": NOTED}).text
        plain = client.post("/parent/inbox/read", data={"text": NO_NOTE}).text

    review = page.split('id="needs-review"', 1)[1].split("</section>", 1)[0]
    assert review.count(TYPING_LOST) == 1
    assert (
        '<button type="submit" class="secondary" formaction="/parent/inbox/edit" formnovalidate '
        'aria-describedby="typing-lost">Edit the text</button>'
    ) in review
    assert page.count('id="typing-lost"') == 1
    assert "typing-lost" not in plain


EDIT_TYPING_LOST = (
    '<p class="helper" id="edit-typing-lost">'
    "Selections, titles, and due dates aren't kept when the text is edited.</p>"
)


def test_the_save_panel_s_edit_says_the_selections_titles_and_dates_are_not_kept(
    tmp_path: pathlib.Path,
) -> None:
    """The save panel's Edit reads the text again as Edit the text does. On a review with
    notes the panel shows the same line beside it once, and the press names it, whether or not
    the page has lines to review. A review with no note has neither."""
    with client_in(tmp_path) as client:
        with_lines = client.post("/parent/inbox/read", data={"text": NOTED}).text
        notes_only = client.post("/parent/inbox/read", data={"text": ONE_UNCLEAR_NOTE}).text
        plain = client.post("/parent/inbox/read", data={"text": NO_NOTE}).text

    assert 'id="needs-review"' in with_lines
    assert 'id="needs-review"' not in notes_only
    for page in (with_lines, notes_only):
        panel = page.split('<aside class="review-aside"', 1)[1].split("</aside>", 1)[0]
        assert panel.count(EDIT_TYPING_LOST) == 1
        assert (
            '<button type="submit" class="secondary" formaction="/parent/inbox/edit" '
            'formnovalidate aria-describedby="edit-typing-lost">Edit</button>'
        ) in panel
        assert page.count('id="edit-typing-lost"') == 1
    assert "edit-typing-lost" not in plain
    assert (
        '<button type="submit" class="secondary" formaction="/parent/inbox/edit" '
        "formnovalidate>Edit</button>"
    ) in plain


def test_the_page_that_reads_no_store_lists_each_title_and_date_as_sent(
    tmp_path: pathlib.Path,
) -> None:
    """With the homework notes selected by Review changes and saved, and a claim on record
    that cannot be read, the page that reads no store says of each note the title and due
    date the form sent, beside its tick, for a save and for a press; a first reading sent
    none, and says none."""
    with client_in(tmp_path) as client:
        page = homework_selected(client, FIELDS)
        mapped, quiz = key_of(page, "Map project"), key_of(page, "Quiz on unit two")
        practice = key_of(page, "Online practice")
        assert client.post("/parent/inbox/keep", data=ticked_form(page)).status_code == 303
        claims_damaged(client)
        form = ticked_form(page) | {
            f"title-{mapped}": "Map of the river",
            f"when-{mapped}": "other",
            f"when_other-{mapped}": "2026-11-06",
            f"title-{practice}": "Online practice",
            f"when-{practice}": "2026-11-10",
            f"title-{quiz}": "Unit two quiz",
            f"when-{quiz}": "none",
        }
        saved = client.post("/parent/inbox/keep", data=form)
        pressed = client.post("/parent/inbox/find", data=form | {"recheck": "1"})
        first = client.post("/parent/inbox/read", data={"text": FIELDS})

    def named(title: str) -> str:
        return f'Title: <span class="authored-text">{title}</span>.'

    def dated(day: str) -> str:
        return f'Due date: <span class="authored-text">{day}</span>.'

    for answer in (saved, pressed):
        assert answer.status_code == 500
        assert (
            f"Card {mapped}: no answer about the assignment; selected to save. "
            f"{named('Map of the river')} {dated('2026-11-06')}</li>"
        ) in answer.text
        assert (
            f"Card {practice}: no answer about the assignment; selected to save. "
            f"{named('Online practice')} {dated('2026-11-10')}</li>"
        ) in answer.text
        assert (
            f"Card {quiz}: no answer about the assignment; not selected. "
            f"{named('Unit two quiz')} Due date: none.</li>"
        ) in answer.text
    assert first.status_code == 500
    assert "Title:" not in first.text
    assert "Due date:" not in first.text


CORRECTED = """Homework for Wren
11/02/2026 - Monday
Humanities -
Study for the vocabulary quiz on Thursday. Map project due on Wednesday.

08 Geometry -
Quiz on unit two

Music -
Unit test today. No additional Music homework assigned.

11/03/2026 - Tuesday
Science -
Online practice due Tuesday

Art -
Poster due Wednesday
"""
"""A note that names a date, a dated note, a note that may be homework, a note that says no
homework is assigned, a note due on a weekday it was first listed on, and another dated
note."""


def test_corrections_stand_after_the_same_text_is_pasted_again(tmp_path: pathlib.Path) -> None:
    """On the page Review changes returns once the family selects the homework notes, a title
    corrected, the named Thursday taken, the second Tuesday chosen, a date cleared and the
    unclear note ticked, reviewed and saved: the same text pasted again shows each of those
    notes already saved, under its saved title and with its saved date, asks nothing, has
    nothing to save, and leaves the note that says no homework unselected."""
    with client_in(tmp_path) as client:
        page = homework_selected(client, CORRECTED)
        study = key_of(page, "Study for the vocabulary quiz on Thursday")
        mapped, quiz = key_of(page, "Map project"), key_of(page, "Quiz on unit two")
        practice, poster = key_of(page, "Online practice"), key_of(page, "Poster")
        named = {study, mapped, quiz, practice, poster}
        offered = numbers(page, r'<input type="hidden" name="offered-(\d+)" value="1">')
        form = ticked_form(page) | {
            f"title-{mapped}": "Map of the river",
            f"when-{study}": "2026-11-05",
            f"when-{practice}": "2026-11-10",
            f"when-{poster}": "none",
            f"take-{quiz}": "1",
            "recheck": "1",
        }
        reviewed = client.post("/parent/inbox/find", data=form)
        kept = client.post("/parent/inbox/keep", data=ticked_form(reviewed.text))
        rows = {row.title: row for row in store_of(client).all_assignments()}
        again = client.post("/parent/inbox/read", data={"text": CORRECTED}).text
        poster_open = details_opened(client, again, "Poster")

    left_out = offered - named
    assert len(left_out) == 1
    assert reviewed.status_code == 200
    assert kept.status_code == 303
    assert kept.headers["location"].endswith("added=5&updated=0&unchanged=0")
    assert {title: row.due_date for title, row in rows.items()} == {
        "Study for the vocabulary quiz on Thursday": datetime.date(2026, 11, 5),
        "Map of the river": datetime.date(2026, 11, 4),
        "Quiz on unit two": None,
        "Online practice": datetime.date(2026, 11, 10),
        "Poster": None,
    }
    for title, due in (
        ("Study for the vocabulary quiz on Thursday", "Due Thursday, November 5, 2026"),
        ("Map project", "Due Wednesday, November 4, 2026"),
        ("Online practice", "Due Tuesday, November 10, 2026"),
    ):
        body = card(again, title)
        assert '<span class="pill">Already saved</span>' in body
        assert due in body
    for title in ("Quiz on unit two", "Poster"):
        body = card(again, title)
        assert '<span class="pill">Already saved</span>' in body
        assert "Due " not in body.split('<p class="effect"', 1)[0]
    assert (
        'It\'s saved here as <span class="authored-text">Map of the river</span>, Humanities, '
        "due Wednesday, November 4, 2026."
    ) in card(again, "Map project")
    poster_choices = when_options(card(poster_open, "Poster"), poster)
    assert poster_choices
    assert not any(chosen for _, chosen in poster_choices)
    for asked in ("identity-question-", "occurrence-question-", "instructions-question-"):
        assert asked not in again
    assert "Everything here is saved already; there is nothing to save." in again
    assert numbers(again, TICKED) == named
    assert left_out <= numbers(again, r'<input type="hidden" name="offered-(\d+)" value="1">')


def test_a_notes_corrected_title_pasted_again_is_already_saved_and_saves_nothing(
    tmp_path: pathlib.Path,
) -> None:
    """A note's title corrected with Edit details, reviewed and saved: the same text pasted
    again shows that note already saved under its corrected title, and Save as the page stands
    writes nothing and adds no row."""
    with client_in(tmp_path) as client:
        page = details_opened(client, homework_selected(client, FIELDS), "Map project")
        mapped = key_of(page, "Map project")
        form = ticked_form(page) | {f"title-{mapped}": "Map of the river", "recheck": "1"}
        reviewed = client.post("/parent/inbox/find", data=form)
        kept = client.post("/parent/inbox/keep", data=ticked_form(reviewed.text))
        first = sorted((row.title, row.due_date) for row in store_of(client).all_assignments())
        again = client.post("/parent/inbox/read", data={"text": FIELDS}).text
        resaved = client.post("/parent/inbox/keep", data=ticked_form(again))
        last = sorted((row.title, row.due_date) for row in store_of(client).all_assignments())

    assert kept.status_code == 303
    assert ("Map of the river", datetime.date(2026, 11, 4)) in first
    assert '<span class="pill">Already saved</span>' in card(again, "Map project")
    assert 'saved here as <span class="authored-text">Map of the river</span>' in card(
        again, "Map project"
    )
    assert resaved.status_code == 303
    assert resaved.headers["location"].endswith("added=0&updated=0&unchanged=3")
    assert last == first


def test_a_folded_note_carries_its_edits_to_the_save(tmp_path: pathlib.Path) -> None:
    """With both notes selected by Review changes, a later note of the same work given
    another date, then answered as the same assignment with its date changed, is folded into
    the earlier card: the page carries its title and date as typed, and the save writes the
    one row with that date as the family's."""
    with client_in(tmp_path) as client:
        page = homework_selected(client, TWICE_NOTED)
        # Typed in the later note's details, opened by its Edit details press.
        form = ticked_form(page) | {
            "title-1": "Reading log",
            "when-1": "other",
            "when_other-1": "2026-11-09",
            "recheck": "1",
        }
        dated = client.post("/parent/inbox/find", data=form)
        folded = client.post(
            "/parent/inbox/find",
            data=ticked_form(dated.text) | {"occurrence-1": "update", "recheck": "1"},
        )
        kept = client.post(
            "/parent/inbox/keep",
            data=ticked_form(folded.text) | {"apply-0-0": "1", "apply-0-1": "1"},
        )
        rows = store_of(client).all_assignments()

    assert (
        '<label><input type="radio" name="occurrence-1" value="update"> The same assignment; '
        "change its due date from Friday, November 6, 2026 to Monday, November 9, 2026</label>"
    ) in dated.text
    assert '<input type="hidden" name="folded-1" value="0">' in folded.text
    assert '<input type="hidden" name="title-1" value="Reading log">' in folded.text
    assert '<input type="hidden" name="when-1" value="other">' in folded.text
    assert '<input type="hidden" name="when_other-1" value="2026-11-09">' in folded.text
    assert kept.status_code == 303
    assert [(row.title, row.due_date, row.origins["due_date"]) for row in rows] == [
        ("Reading log", datetime.date(2026, 11, 9), SourceChannel.PARENT_ENTRY)
    ]


ONE_HOMEWORK_NOTE = "Homework for Wren\n11/02/2026 - Monday\nScience -\nReading log due today\n"
"""A day whose only content is a note read as homework."""


def found_line(page: str) -> str:
    """The line of a review that says how many notes were read and how many are selected."""
    found: list[str] = re.findall(r'<p class="note" id="notes-found">([^<]*)</p>', page)
    assert len(found) == 1
    return html.unescape(found[0])


def test_every_note_starts_unselected_whatever_its_standing(tmp_path: pathlib.Path) -> None:
    """Each reading made from a teacher's note starts unselected, homework or not, dated or
    not, and its card says what selecting it does. The cards start as they do. The page tells
    the notes read apart from the notes selected; a paste whose only note is homework says no
    assignment is selected and offers Review changes and Save."""
    with client_in(tmp_path) as client:
        page = client.post("/parent/inbox/read", data={"text": NOTED}).text
        alone = client.post("/parent/inbox/read", data={"text": ONE_HOMEWORK_NOTE}).text
        plain = client.post("/parent/inbox/read", data={"text": NO_NOTE}).text

    assert numbers(page, r'<input type="checkbox" name="take-(\d+)" value="1"') == {2, 3, 4, 5, 6}
    assert numbers(page, TICKED) == set()
    assert page.count("If selected: ") == 6
    assert card(page, "Short essay").count("If selected: ") == 1
    assert found_line(page) == "Found 6 items in the teacher's notes; none is selected yet."
    assert "2 assignments across 1 week: 2 new, 0 updates, 0 already saved." in " ".join(
        page.split()
    )
    assert f'<p class="empty">{NOTHING_SELECTED}</p>' in alone
    assert found_line(alone) == "Found 1 item in the teacher's notes; none is selected yet."
    assert alone.count(REVIEW_CHANGES) == 1
    assert alone.count(SAVE_SELECTED) == 1
    assert 'id="notes-found"' not in plain


def test_a_saved_note_read_again_shows_saved_and_selected(tmp_path: pathlib.Path) -> None:
    """A note selected and saved, read again in the same text, is saved already and
    selected; the notes not saved start unselected, and the line counts the one selected."""
    with client_in(tmp_path) as client:
        page = client.post("/parent/inbox/read", data={"text": NOTED}).text
        kept = client.post("/parent/inbox/keep", data=ticked_form(page) | {"take-4": "1"})
        again = client.post("/parent/inbox/read", data={"text": NOTED}).text

    assert kept.headers["location"].endswith("added=3&updated=0&unchanged=0")
    assert '<span class="pill">Already saved</span>' in card(again, "Reading log")
    assert numbers(again, TICKED) == {4}
    assert found_line(again) == "Found 6 items in the teacher's notes; 1 is selected."


def test_save_with_nothing_selected_writes_nothing(tmp_path: pathlib.Path) -> None:
    """A note selected on Review changes, unticked again and saved, writes nothing: the
    review comes back saying no assignment is selected and how many notes were read, with
    the ticks and the typing as sent and Review changes offered."""
    with client_in(tmp_path) as client:
        page = client.post("/parent/inbox/read", data={"text": ONE_HOMEWORK_NOTE}).text
        chosen = client.post(
            "/parent/inbox/find", data=ticked_form(page) | {"take-0": "1", "recheck": "1"}
        ).text
        form = ticked_form(chosen) | {"title-0": "Reading log for the week"}
        del form["take-0"]
        kept = client.post("/parent/inbox/keep", data=form)
        rows = store_of(client).all_assignments()

    assert SAVE_SELECTED in chosen
    assert rows == []
    assert kept.status_code == 200
    assert kept.text.count(f'<p class="empty">{NOTHING_SELECTED}</p>') == 1
    assert found_line(kept.text) == "Found 1 item in the teacher's notes; none is selected yet."
    assert numbers(kept.text, TICKED) == set()
    assert numbers(kept.text, r'<input type="hidden" name="offered-(\d+)" value="1">') == {0}
    assert 'name="title-0" value="Reading log for the week"' in kept.text
    assert kept.text.count(REVIEW_CHANGES) == 1


def test_a_tick_survives_review_changes_a_refusal_a_retry_and_a_restart(
    tmp_path: pathlib.Path,
) -> None:
    """A note the family selects stays selected through Review changes, a review refused
    because the application restarted, the same form sent again, and the page handed back,
    which saves it in one press."""
    with client_in(tmp_path) as client:
        page = client.post("/parent/inbox/read", data={"text": ONE_HOMEWORK_NOTE}).text
        chosen = client.post(
            "/parent/inbox/find", data=ticked_form(page) | {"take-0": "1", "recheck": "1"}
        ).text
    form = ticked_form(chosen)
    with client_in(tmp_path) as client:
        refused = client.post("/parent/inbox/keep", data=form)
        retried = client.post("/parent/inbox/keep", data=form)
        unwritten = store_of(client).all_assignments()
        kept = client.post("/parent/inbox/keep", data=ticked_form(retried.text))
        rows = [row.title for row in store_of(client).all_assignments()]

    assert numbers(chosen, TICKED) == {0}
    assert (refused.status_code, retried.status_code) == (409, 409)
    assert numbers(refused.text, TICKED) == numbers(retried.text, TICKED) == {0}
    assert unwritten == []
    assert kept.status_code == 303
    assert rows == ["Reading log"]


DECEMBER = "Homework for Wren\n12/15/2026 - Tuesday\nLanguage Arts -\nEssay due January 8\n"
"""A note under a December school day that names a January date with no year."""


def test_a_january_date_in_a_december_note_is_shown_with_the_following_year(
    tmp_path: pathlib.Path,
) -> None:
    """The review shows a January date read under a December school day with its year, the
    following one, on the card and in its due-date choice."""
    with client_in(tmp_path, today="2026-12-15") as client:
        page = client.post("/parent/inbox/read", data={"text": DECEMBER}).text
        page = details_opened(client, page, "Essay")

    body = " ".join(card(page, "Essay").split())
    assert "Due Friday, January 8, 2027" in body
    assert when_options(card(page, "Essay"), key_of(page, "Essay"))[0] == ("2027-01-08", True)


def test_a_saved_family_date_stands_when_a_december_note_is_read_again(
    tmp_path: pathlib.Path,
) -> None:
    """A December note selected with a date the family chose and saved, pasted again in
    December, is already saved with the family's date, and a save keeps it."""
    with client_in(tmp_path, today="2026-12-15") as client:
        page = client.post("/parent/inbox/read", data={"text": DECEMBER}).text
        essay = key_of(page, "Essay")
        form = ticked_form(page) | {
            f"take-{essay}": "1",
            f"when-{essay}": "other",
            f"when_other-{essay}": "2027-01-11",
            "recheck": "1",
        }
        reviewed = client.post("/parent/inbox/find", data=form).text
        kept = client.post("/parent/inbox/keep", data=ticked_form(reviewed))
        again = client.post("/parent/inbox/read", data={"text": DECEMBER}).text
        resaved = client.post("/parent/inbox/keep", data=ticked_form(again))
        rows = [(row.title, row.due_date) for row in store_of(client).all_assignments()]

    assert kept.headers["location"].endswith("added=1&updated=0&unchanged=0")
    body = " ".join(card(again, "Essay").split())
    assert '<span class="pill">Already saved</span>' in body
    assert "Due Monday, January 11, 2027" in body
    assert resaved.headers["location"].endswith("added=0&updated=0&unchanged=1")
    assert rows == [("Essay", datetime.date(2027, 1, 11))]


MIXED = (
    "Homework for Wren\n11/02/2026 - Monday\nLanguage Arts -\n"
    "No homework due tomorrow, but the essay is due Friday.\n"
)
"""A note that says no homework is due tomorrow and sets a deadline for Friday."""


def test_a_no_homework_statement_with_a_later_deadline_may_be_homework_with_its_date(
    tmp_path: pathlib.Path,
) -> None:
    """A note that says no homework is due tomorrow but the essay is due Friday is under
    the notes that may be homework, unselected, with that Friday offered as its due date."""
    with client_in(tmp_path, today="2026-11-02") as client:
        page = client.post("/parent/inbox/read", data={"text": MIXED}).text
        (only,) = titles(sections(page)[MAY_BE])
        page = details_opened(client, page, only)

    shown = sections(page)
    assert list(shown) == [MAY_BE]
    (title,) = titles(shown[MAY_BE])
    assert numbers(page, TICKED) == set()
    assert ("2026-11-06", False) in when_options(card(page, title), key_of(page, title))


@pytest.mark.parametrize(
    ("when", "said"),
    [
        ({}, True),
        ({"when-{key}": "none"}, False),
        ({"when-{key}": "other", "when_other-{key}": "2026-11-09"}, False),
        ({"when-{key}": "other", "when_other-{key}": "2026-11-04"}, True),
    ],
    ids=["as-read", "cleared", "family-date", "family-date-equal-to-the-note-s"],
)
def test_the_listed_again_line_names_the_due_date_only_while_it_is_the_note_s(
    tmp_path: pathlib.Path, when: dict[str, str], said: bool
) -> None:
    """The lines that a note is listed again after its deadline name that deadline, and are
    on its card only while its due date is the note's own: as read, or chosen again as the
    same day; a cleared date or another date the family chose takes them off."""
    with client_in(tmp_path, today="2026-11-06") as client:
        page = client.post("/parent/inbox/read", data={"text": LISTED_AFTER}).text
        key = key_of(page, "Map project")
        chosen = {name.format(key=key): value for name, value in when.items()}
        form = ticked_form(page) | {f"take-{key}": "1", "recheck": "1"} | chosen
        reviewed = client.post("/parent/inbox/find", data=form).text

    shown = " ".join(card(reviewed, "Map project").split())
    lines = [
        '<p class="source">Still listed on Thursday, November 5, 2026, after its due date of '
        "Wednesday, November 4, 2026.</p>",
        '<p class="source">Still listed on Friday, November 6, 2026, after its due date of '
        "Wednesday, November 4, 2026.</p>",
    ]
    assert [line in shown for line in lines] == [said, said]
    assert reviewed.count("Still listed on") == (2 if said else 0)


def due_line(body: str) -> str:
    """The due-date line of a card, single-spaced."""
    found = re.search(r'<p class="due">(.*?)</p>', body, re.S)
    assert found is not None
    return " ".join(re.sub(r"<[^>]+>", " ", found.group(1)).split())


UNDATED_CARD = (
    "Assignments:\n11/02 08 Geometry - A: Homework/Classwork: Book Covers Grade: Missing\n"
)
"""A card the school reports missing, with no due date."""


def test_a_note_with_no_due_date_says_no_due_date_and_a_card_says_none_is_in_the_text(
    tmp_path: pathlib.Path,
) -> None:
    """A reading made from a note with no due date says "No due date", whether the note
    names none or the family cleared the date it names; a card with no due date says none is
    in the text."""
    with client_in(tmp_path, today="2026-11-06") as client:
        page = client.post("/parent/inbox/read", data={"text": ONE_UNCLEAR_NOTE}).text
        listed = client.post("/parent/inbox/read", data={"text": LISTED_AFTER}).text
        key = key_of(listed, "Map project")
        cleared = client.post(
            "/parent/inbox/find",
            data=ticked_form(listed) | {f"take-{key}": "1", f"when-{key}": "none", "recheck": "1"},
        ).text
        plain = client.post("/parent/inbox/read", data={"text": UNDATED_CARD}).text

    assert due_line(card(page, "Quiz on unit two")) == "No due date"
    assert due_line(card(cleared, "Map project")) == "No due date"
    assert due_line(card(plain, "Book Covers")) == "No due date in the text"


MAP_PROJECT = """Homework for Wren
11/02/2026 - Monday
Humanities -
Map project due on Wednesday.

11/05/2026 - Thursday
Humanities - Assigned: Map project: (Due:11/12/2026)
Humanities -
Map project due on Wednesday.
"""
"""A note listed on a Monday and again on the Thursday, beside the Thursday's card of its
name, which is due a week after the note's own deadline: the note joins no card."""


def card_with(page: str, field: str) -> str:
    """The card on a page that holds a field of this name."""
    cards: list[str] = re.findall(r"<article [^>]*>.*?</article>", page, re.S)
    found = [shown for shown in cards if f'name="{field}"' in shown]
    assert len(found) == 1, field
    return found[0]


def test_a_note_that_joins_no_card_of_its_name_asks_same_or_new_with_both_dates(
    tmp_path: pathlib.Path,
) -> None:
    """With nothing saved, a note that joins no card of its name in the same text is a
    reading of its own and starts unselected. Its card shows the card's due date and the
    note's, each with where it came from, and asks whether it is the same assignment or new
    work, with nothing chosen. An answer given before the note is selected comes back with
    Review changes. Selected and answered new work, the page saves both in one press; left
    unselected, the Save as shown writes the card alone."""
    with client_in(tmp_path, today="2026-11-05") as client:
        page = client.post("/parent/inbox/read", data={"text": MAP_PROJECT}).text
        answered = client.post(
            "/parent/inbox/find",
            data=ticked_form(page) | {"occurrence-1": "new", "recheck": "1"},
        ).text
        kept = client.post(
            "/parent/inbox/keep", data=ticked_form(page) | {"take-1": "1", "occurrence-1": "new"}
        )
        rows = sorted((row.title, row.due_date) for row in store_of(client).all_assignments())
    with client_in(tmp_path / "alone", today="2026-11-05") as client:
        alone = client.post(
            "/parent/inbox/keep",
            data=ticked_form(client.post("/parent/inbox/read", data={"text": MAP_PROJECT}).text),
        )
        written = [(row.title, row.due_date) for row in store_of(client).all_assignments()]

    noted = " ".join(card_with(page, "take-1").split())
    radio = '<label><input type="radio" name="occurrence-1" value="{}"> {}</label>'
    assert numbers(page, TICKED) == set()
    assert "<dt>Also due in this text</dt> <dd>Thursday, November 12, 2026</dd>" in noted
    assert (
        "<dt>Pasted due date</dt> <dd>Wednesday, November 4, 2026 (school portal (the "
        "teacher&#39;s note)"
    ) in noted
    assert (
        radio.format(
            "keep", "The same assignment; keep its due date of Thursday, November 12, 2026"
        )
        in noted
    )
    assert (
        radio.format(
            "update",
            "The same assignment; change its due date from Thursday, November 12, 2026 to "
            "Wednesday, November 4, 2026",
        )
        in noted
    )
    assert radio.format("new", "New work under the same name") in noted
    assert not re.search(r'name="occurrence-1" value="[a-z]+" checked', page)
    assert 'name="occurrence-0"' not in page
    assert numbers(answered, TICKED) == set()
    assert re.findall(r'name="occurrence-1" value="([a-z]+)" checked', answered) == ["new"]
    assert kept.status_code == 303
    assert kept.headers["location"].endswith("added=2&updated=0&unchanged=0")
    assert rows == [
        ("Map project", datetime.date(2026, 11, 4)),
        ("Map project", datetime.date(2026, 11, 12)),
    ]
    assert alone.status_code == 303
    assert alone.headers["location"].endswith("added=1&updated=0&unchanged=0")
    assert written == [("Map project", datetime.date(2026, 11, 12))]


DEFAULT_PRESS = (
    '<button type="submit" class="visually-hidden" formaction="/parent/inbox/find" '
    'formnovalidate name="recheck" value="1" tabindex="-1" aria-hidden="true">'
    "Review changes</button>"
)
"""The first submit button of a review form with note title or date boxes: Review changes'
request, out of the tab order and hidden from assistive technology, so Enter in a box shows
the review again with what was typed."""


def first_press(page: str) -> str:
    """The first submit button of the review form, in tree order."""
    form = re.search(r'<form method="post" action="/parent/inbox/keep".*?</form>', page, re.S)
    assert form is not None
    found = re.search(r"<button [^>]*>.*?</button>", form.group(0), re.S)
    assert found is not None
    return found.group(0)


def request_of(button: str) -> dict[str, str]:
    """What a submit button sends: its form action, its skipped validation, name and value."""
    attributes = dict(re.findall(r'\s([\w-]+)(?:="([^"]*)")?', button.partition(">")[0]))
    return {
        name: attributes[name]
        for name in ("formaction", "formnovalidate", "name", "value")
        if name in attributes
    }


def test_the_first_press_of_every_form_with_note_boxes_is_review_changes(
    tmp_path: pathlib.Path,
) -> None:
    """On every review that shows a note's title or date boxes, its details opened (with and
    without text that needs review, after Review changes, an edit pressed with Save, a
    refused date and a page the application did not sign), the form's first submit button
    sends Review changes' request, so Enter in a box never sends Edit the text, Find or Save,
    and no Save is offered."""
    with client_in(tmp_path) as client:
        noted = client.post("/parent/inbox/read", data={"text": NOTED}).text
        unread = details_opened(client, noted, "Reading log")
        first = client.post("/parent/inbox/read", data={"text": ONE_HOMEWORK_NOTE}).text
        alone = details_opened(client, first, "Reading log")
        reviewed = details_opened(client, homework_selected(client, NOTED), "Reading log")
        fields = details_opened(client, homework_selected(client, FIELDS), "Map project")
        mapped = key_of(fields, "Map project")
        edited = client.post(
            "/parent/inbox/keep", data=ticked_form(fields) | {f"title-{mapped}": "Map of the river"}
        )
        far = client.post(
            "/parent/inbox/keep",
            data=ticked_form(fields)
            | {f"when-{mapped}": "other", f"when_other-{mapped}": "2028-11-04"},
        )
        chosen = opened(client, first, 0, **{"take-0": "1"}).text
    with client_in(tmp_path) as client:
        restarted = client.post("/parent/inbox/keep", data=ticked_form(chosen))

    assert 'id="needs-review"' in unread
    assert 'id="needs-review"' not in alone
    assert (edited.status_code, far.status_code, restarted.status_code) == (200, 422, 409)
    for page in (unread, alone, reviewed, fields, edited.text, far.text, restarted.text):
        assert 'type="text" name="title-' in page
        assert SAVE_SELECTED not in page
        assert first_press(page) == DEFAULT_PRESS
        assert page.count(DEFAULT_PRESS) == 1
        assert page.count(REVIEW_CHANGES) == 1
    assert request_of(DEFAULT_PRESS) == request_of(REVIEW_CHANGES)
    assert request_of(DEFAULT_PRESS) == {
        "formaction": "/parent/inbox/find",
        "formnovalidate": "",
        "name": "recheck",
        "value": "1",
    }


def test_enter_in_a_title_box_keeps_the_title_and_the_tick_and_writes_nothing(
    tmp_path: pathlib.Path,
) -> None:
    """The first press of a review with text that needs review and a card's details open,
    sent with a title typed and a note ticked, as Enter in the title box sends it: the review
    comes back with the title reviewed and the tick, and nothing is written."""
    with client_in(tmp_path) as client:
        first = client.post("/parent/inbox/read", data={"text": NOTED}).text
        page = details_opened(client, first, "Reading log")
        log = key_of(page, "Reading log")
        sent = ticked_form(page) | {f"title-{log}": "Reading log for the week", f"take-{log}": "1"}
        press = request_of(first_press(page))
        entered = client.post(press["formaction"], data=sent | {press["name"]: press["value"]})
        rows = store_of(client).all_assignments()

    assert entered.status_code == 200
    assert rows == []
    assert f'<input type="hidden" name="title-{log}" value="Reading log for the week">' in (
        entered.text
    )
    assert reviewed_line("Reading log for the week", "Monday, November 2, 2026") in entered.text
    assert log in numbers(entered.text, TICKED)
    assert 'id="needs-review"' in entered.text


FAR_NAMED = "Homework for Wren\n12/15/2026 - Tuesday\nHumanities -\nEssay due January 8, 2028\n"
"""A note under a December school day whose deadline is written with a year more than a year
from the day it is read."""

FAR_NAMED_LINE = (
    '<p class="source">The note names Saturday, January 8, 2028. It can&#39;t be saved as the '
    "due date because it&#39;s more than a year away.</p>"
)


@pytest.mark.parametrize(
    "text", [FAR_NAMED, FAR_NAMED.replace("8, 2028", "8,2028")], ids=["space", "comma"]
)
def test_a_named_date_the_due_date_check_refuses_is_shown_as_the_notes_not_as_a_choice(
    tmp_path: pathlib.Path, text: str
) -> None:
    """A date the note names with its written year that the due-date check refuses is said on
    its card, with its year and why it can't be the due date, and is no choice: the choices
    are no due date, chosen, and another date. The note's words stay as written."""
    with client_in(tmp_path, today="2026-12-15") as client:
        page = client.post("/parent/inbox/read", data={"text": text}).text
        reviewed = page
        page = details_opened(client, page, "Essay")

    body = card(page, "Essay")
    key = key_of(page, "Essay")
    assert body.count(FAR_NAMED_LINE) == 1
    assert when_options(body, key) == [("none", True), ("other", False)]
    assert 'value="2028-01-08"' not in body
    assert text.splitlines()[3] in html.unescape(body)
    assert "2027" not in body
    assert card(reviewed, "Essay").count(FAR_NAMED_LINE) == 1


def test_a_far_named_date_leaves_another_date_and_no_due_date_to_save(
    tmp_path: pathlib.Path,
) -> None:
    """The note whose named date can't be the due date, ticked with no due date, saves in one
    press with no due date; ticked with another date within a year, reviewed and saved, it
    saves that date as the family's entry."""
    with client_in(tmp_path, today="2026-12-15") as client:
        page = client.post("/parent/inbox/read", data={"text": FAR_NAMED}).text
        key = key_of(page, "Essay")
        none = client.post("/parent/inbox/keep", data=ticked_form(page) | {f"take-{key}": "1"})
        undated = [(row.title, row.due_date) for row in store_of(client).all_assignments()]
    (tmp_path / "other").mkdir()
    with client_in(tmp_path / "other", today="2026-12-15") as client:
        page = client.post("/parent/inbox/read", data={"text": FAR_NAMED}).text
        form = ticked_form(page) | {
            f"take-{key}": "1",
            f"when-{key}": "other",
            f"when_other-{key}": "2027-01-08",
            "recheck": "1",
        }
        reviewed = client.post("/parent/inbox/find", data=form)
        kept = client.post("/parent/inbox/keep", data=ticked_form(reviewed.text))
        rows = store_of(client).all_assignments()

    assert none.status_code == 303
    assert undated == [("Essay", None)]
    assert reviewed.status_code == 200
    assert reviewed.text.count(FAR_NAMED_LINE) == 1
    assert kept.status_code == 303
    assert [(row.title, row.due_date, row.origins["due_date"]) for row in rows] == [
        ("Essay", datetime.date(2027, 1, 8), SourceChannel.PARENT_ENTRY)
    ]


def test_a_review_of_new_notes_offers_save_with_nothing_selected(tmp_path: pathlib.Path) -> None:
    """A paste whose only content is a note read as homework offers Save on its first view,
    beside Review changes, with the note unselected. Ticked and saved, the note is written in
    one press; saved with nothing selected, nothing is written and the review comes back
    saying no assignment is selected; ticked with its title changed and saved, nothing is
    written and the review comes back with the title and the tick as typed."""
    with client_in(tmp_path) as client:
        page = client.post("/parent/inbox/read", data={"text": ONE_HOMEWORK_NOTE}).text
        nothing = client.post("/parent/inbox/keep", data=ticked_form(page))
        unwritten = store_of(client).all_assignments()
        edited = client.post(
            "/parent/inbox/keep",
            data=ticked_form(page) | {"take-0": "1", "title-0": "Reading log for the week"},
        )
        still = store_of(client).all_assignments()
        kept = client.post("/parent/inbox/keep", data=ticked_form(page) | {"take-0": "1"})
        rows = [(row.title, row.due_date) for row in store_of(client).all_assignments()]

    assert page.count(SAVE_SELECTED) == 1
    assert page.count(REVIEW_CHANGES) == 1
    assert f'<p class="empty">{NOTHING_SELECTED}</p>' in page
    assert numbers(page, TICKED) == set()
    assert nothing.status_code == 200
    assert unwritten == []
    assert nothing.text.count(f'<p class="empty">{NOTHING_SELECTED}</p>') == 1
    assert nothing.text.count(SAVE_SELECTED) == 1
    assert nothing.text.count(REVIEW_CHANGES) == 1
    assert edited.status_code == 200
    assert still == []
    assert f"{SUMMARY}{EDITS_IN_REVIEW}" in edited.text
    assert 'name="title-0" value="Reading log for the week"' in edited.text
    assert numbers(edited.text, TICKED) == {0}
    assert kept.status_code == 303
    assert kept.headers["location"].endswith("added=1&updated=0&unchanged=0")
    assert rows == [("Reading log", datetime.date(2026, 11, 2))]


TWO_JOINED = NOTED.replace(
    "Short essay Due.", "Short essay due Friday.\n\nShort essay due Thursday."
)
"""The two days, with two notes above the Tuesday's card, each naming a deadline of its own:
the Friday and the Thursday."""


@pytest.mark.parametrize(
    ("ticks", "dates"),
    [
        (("join-1-0",), ["2026-11-06"]),
        (("join-1-1",), ["2026-11-05"]),
        (("join-1-0", "join-1-1"), ["2026-11-05", "2026-11-06"]),
    ],
    ids=["first", "second", "both"],
)
def test_two_joined_notes_of_one_card_ticked_alone_or_together_save_in_one_press(
    tmp_path: pathlib.Path, ticks: tuple[str, ...], dates: list[str]
) -> None:
    """Two notes joined to one card, shown unselected: either one ticked alone, or both
    ticked, and pressed with Save write in that one press what the page said each does if
    selected, and the same press sent again writes nothing more."""
    with client_in(tmp_path) as client:
        page = client.post("/parent/inbox/read", data={"text": TWO_JOINED}).text
        form = ticked_form(page) | dict.fromkeys(ticks, "1")
        saved = client.post("/parent/inbox/keep", data=form)
        claimed = teacher_note_claims(client)
        counts = record_counts(client)
        again = client.post("/parent/inbox/keep", data=form)
        claimed_again = teacher_note_claims(client)
        counts_again = record_counts(client)

    assert joined_ticks(card(page, "Short essay")) == {"join-1-0": False, "join-1-1": False}
    assert card(page, "Short essay").count("If selected: ") == 2
    assert saved.status_code == 303
    assert sorted(claimed["Short essay"]) == dates
    assert again.status_code == 303
    assert (claimed_again, counts_again) == (claimed, counts)


def test_two_joined_ticks_saved_against_a_record_moved_under_the_page_write_nothing(
    tmp_path: pathlib.Path,
) -> None:
    """The card saved from another tab after the page was shown: both joined notes ticked
    and pressed with Save would add to a saved row, not make the new one the page said, so
    nothing is written and the review comes back saying it does not match what saving would
    do, with the ticks as sent."""
    with client_in(tmp_path) as client:
        page = client.post("/parent/inbox/read", data={"text": TWO_JOINED}).text
        assert client.post("/parent/inbox/keep", data=ticked_form(page)).status_code == 303
        before = record_counts(client)
        moved = client.post(
            "/parent/inbox/keep", data=ticked_form(page) | {"join-1-0": "1", "join-1-1": "1"}
        )
        after = record_counts(client)
        claimed = teacher_note_claims(client)

    assert moved.status_code == 409
    assert REVIEW_NO_LONGER_MATCHES in moved.text
    assert SELECTIONS_CHANGE not in moved.text
    assert after == before
    assert claimed["Short essay"] == []
    assert joined_ticks(card(moved.text, "Short essay")) == {"join-1-0": True, "join-1-1": True}


ESSAY_WEEK = (
    "Homework for Wren\n11/09/2026 - Monday\nHumanities -\n"
    "Essay due November 20. Write an outline.\n"
)
"""A Monday whose only content is a note that settles its deadline, the Friday a week on."""

ESSAY_NEXT_WEEK = ESSAY_WEEK.replace("11/09/2026", "11/16/2026")
"""The same words listed under the next Monday: another occurrence of the note."""

ALREADY_SAVED = '<span class="pill">Already saved</span>'


def test_a_saved_note_read_under_a_later_week_is_new_work_not_already_saved(
    tmp_path: pathlib.Path,
) -> None:
    """A note saved under one Monday and read again under that Monday is already saved,
    selected, and its Save writes nothing. The same words, title and deadline read under the
    next Monday are not that note: unselected, not already saved, asked nothing, and ticked
    and saved they add new work beside the first."""
    with client_in(tmp_path, today="2026-11-16") as client:
        first = client.post("/parent/inbox/read", data={"text": ESSAY_WEEK}).text
        saved = client.post("/parent/inbox/keep", data=ticked_form(first) | {"take-0": "1"})
        before_repeat = record_counts(client)
        repeat = client.post("/parent/inbox/read", data={"text": ESSAY_WEEK}).text
        repeated = client.post("/parent/inbox/keep", data=ticked_form(repeat))
        after_repeat = record_counts(client)
        later = client.post("/parent/inbox/read", data={"text": ESSAY_NEXT_WEEK}).text
        later_kept = client.post("/parent/inbox/keep", data=ticked_form(later) | {"take-0": "1"})
        rows = sorted((row.title, row.due_date) for row in store_of(client).all_assignments())

    assert saved.status_code == 303
    assert saved.headers["location"].endswith("added=1&updated=0&unchanged=0")
    assert ALREADY_SAVED in card(repeat, "Essay")
    assert numbers(repeat, TICKED) == {0}
    assert repeated.status_code == 303
    assert repeated.headers["location"].endswith("added=0&updated=0&unchanged=1")
    assert after_repeat == before_repeat
    assert after_repeat[0] == 1
    assert ALREADY_SAVED not in card(later, "Essay")
    assert numbers(later, TICKED) == set()
    assert 'name="identity-0"' not in later
    assert later_kept.status_code == 303
    assert later_kept.headers["location"].endswith("added=1&updated=0&unchanged=0")
    assert rows == [("Essay", datetime.date(2026, 11, 20))] * 2


SENT_TWICE = (
    "A choice on this review was sent twice with different answers, so nothing was saved. "
    "Choose it once, then save."
)
CHECK_CARD = '<a href="#card-{key}">Check this assignment</a></p>'


def sent_as_pairs(client: TestClient, path: str, fields: list[tuple[str, str]]) -> Answer:
    """A form posted with its fields in this order, a field repeated where it is listed twice."""
    return client.post(
        f"/parent/inbox/{path}",
        content=urlencode(fields),
        headers={"Content-Type": "application/x-www-form-urlencoded"},
    )


TWICE = [
    pytest.param("take-4", ("1", "0"), 4, id="take-ticked-first"),
    pytest.param("take-4", ("0", "1"), 4, id="take-unticked-first"),
    pytest.param("join-1-0", ("1", "0"), 1, id="join-ticked-first"),
    pytest.param("join-1-0", ("0", "1"), 1, id="join-unticked-first"),
]
"""A note's tick and a joined note's tick, each sent twice with two different answers, in both
orders, with the card each names."""


@pytest.mark.parametrize(("name", "values", "key"), TWICE)
def test_a_tick_sent_twice_with_different_answers_saves_nothing_and_names_its_card(
    tmp_path: pathlib.Path, name: str, values: tuple[str, str], key: int
) -> None:
    """The review's form with one tick sent twice, once ticked and once not, beside another
    note ticked and its title typed: nothing of the paste is written, the review comes back
    with the other tick and the typed title as sent, the tick sent twice unticked whichever
    came first, and the summary says why with a link to the card that holds it."""
    with client_in(tmp_path) as client:
        page = client.post("/parent/inbox/read", data={"text": NOTED}).text
        others = ticked_form(page) | {"take-3": "1", "title-3": "Map project outline"}
        fields = [*others.items(), *((name, value) for value in values)]
        refused = sent_as_pairs(client, "keep", fields)
        counts = record_counts(client)

    assert refused.status_code == 422
    assert counts == (0, 0, 0)
    assert f"{SUMMARY}{SENT_TWICE} {CHECK_CARD.format(key=key)}" in refused.text
    assert numbers(refused.text, TICKED) == {3}
    assert 'name="title-3" value="Map project outline"' in refused.text
    joined = card(refused.text, "Short essay")
    assert joined_ticks(joined) == ({} if name.startswith("join") else {"join-1-0": False})
    assert unresolved_control(joined, "join-1-0") == name.startswith("join")


@pytest.mark.parametrize(("name", "values", "key"), TWICE)
def test_review_changes_with_a_tick_sent_twice_takes_neither_answer(
    tmp_path: pathlib.Path, name: str, values: tuple[str, str], key: int
) -> None:
    """Review changes with a tick sent twice, once ticked and once not, writes nothing, says
    so with a link to the card that holds it, and shows that tick unticked whichever came
    first, with the other tick as sent."""
    with client_in(tmp_path) as client:
        page = client.post("/parent/inbox/read", data={"text": NOTED}).text
        others = ticked_form(page) | {"take-3": "1", "recheck": "1"}
        fields = [*others.items(), *((name, value) for value in values)]
        shown = sent_as_pairs(client, "find", fields)
        rows = store_of(client).all_assignments()

    assert shown.status_code == 422
    assert rows == []
    assert f"{SUMMARY}{SENT_TWICE} {CHECK_CARD.format(key=key)}" in shown.text
    said = TICK_TWICE if name.startswith("take") else JOIN_TWICE
    assert unescaped(shown.text).count(said) == 1
    assert numbers(shown.text, TICKED) == {3}
    joined = card(shown.text, "Short essay")
    assert joined_ticks(joined) == ({} if name.startswith("join") else {"join-1-0": False})
    assert unresolved_control(joined, "join-1-0") == name.startswith("join")


@pytest.mark.parametrize(
    ("name", "added"),
    [("take-4", 3), ("join-1-0", 2), ("offered-4", 2)],
    ids=["take", "join", "offered"],
)
def test_a_tick_sent_twice_with_the_same_answer_saves_in_one_press(
    tmp_path: pathlib.Path, name: str, added: int
) -> None:
    """A tick, a joined note's tick, or a note's offered field sent twice with the same answer
    is one answer: the save writes in one press, as with it sent once."""
    with client_in(tmp_path) as client:
        page = client.post("/parent/inbox/read", data={"text": NOTED}).text
        form = ticked_form(page) | {name: "1"}
        fields = [*form.items(), (name, form[name])]
        saved = sent_as_pairs(client, "keep", fields)

    assert saved.status_code == 303
    assert saved.headers["location"].endswith(f"added={added}&updated=0&unchanged=0")


EDIT_DETAILS = (
    '<button type="submit" class="secondary" formaction="/parent/inbox/find" formnovalidate '
    'name="details" value="{key}">Edit details<span class="visually-hidden"> for {title}, '
)
"""The start of a note card's Edit details press: the review again with that card's title box
and due-date choices open."""


def reviewed_line(title: str, due: str) -> str:
    """What a note card in the save-ready review says of its title and due date."""
    return (
        f'<p class="source">Title: <span class="authored-text">{title}</span>. Due date: {due}.</p>'
    )


def opened(client: TestClient, page: str, key: int, **sent: str) -> Answer:
    """The review with one card's details opened by its Edit details press, from this page
    sent back as shown with these fields changed."""
    return client.post("/parent/inbox/find", data=ticked_form(page) | sent | {"details": str(key)})


def test_the_first_view_shows_each_notes_title_and_date_as_text_with_edit_details(
    tmp_path: pathlib.Path,
) -> None:
    """On the first view every note card shows its title and due date as reviewed text, the
    dates the note leaves open as source information, one Edit details press and its tick,
    and no title box or date choice; the action is Save selected assignments."""
    with client_in(tmp_path) as client:
        noted = client.post("/parent/inbox/read", data={"text": NOTED}).text
        fields = client.post("/parent/inbox/read", data={"text": FIELDS}).text

    for page in (noted, fields):
        keys = numbers(page, r'name="offered-(\d+)"')
        assert keys
        for key in keys:
            for head in ("title", "when", "when_other"):
                assert f'name="{head}-{key}"' not in page
            assert page.count(f'name="details" value="{key}"') == 1
        assert page.count(">Edit details<") == len(keys)
        assert 'type="radio" name="when-' not in page
        assert page.count(SAVE_SELECTED) == 1
    for title in ("Map project", "Quiz on unit two", "Online practice"):
        key = key_of(fields, title)
        body = card(fields, title)
        assert EDIT_DETAILS.format(key=key, title=title) in body
        assert f'<input type="checkbox" name="take-{key}" value="1">' in body
    assert reviewed_line("Map project", "Wednesday, November 4, 2026") in card(
        fields, "Map project"
    )
    assert reviewed_line("Quiz on unit two", "No due date yet") in card(fields, "Quiz on unit two")
    assert (
        '<p class="source">The note leaves the due date open: Tuesday, November 3, 2026 or '
        "Tuesday, November 10, 2026.</p>"
    ) in card(fields, "Online practice")


def test_a_tick_on_the_first_view_saves_once_and_nothing_selected_saves_nothing(
    tmp_path: pathlib.Path,
) -> None:
    """A note ticked on the first view and saved is written in one press, and the same press
    again writes nothing more; a review of notes saved with nothing selected writes nothing
    and says no assignment is selected."""
    with client_in(tmp_path) as client:
        page = client.post("/parent/inbox/read", data={"text": NOTED}).text
        logged = key_of(page, "Reading log")
        form = ticked_form(page) | {f"take-{logged}": "1"}
        kept = client.post("/parent/inbox/keep", data=form)
        again = client.post("/parent/inbox/keep", data=form)
        rows = sorted(row.title for row in store_of(client).all_assignments())
    with client_in(tmp_path / "alone") as client:
        alone = client.post("/parent/inbox/read", data={"text": ONE_HOMEWORK_NOTE}).text
        nothing = client.post("/parent/inbox/keep", data=ticked_form(alone))
        unwritten = store_of(client).all_assignments()

    assert kept.status_code == 303
    assert kept.headers["location"].endswith("added=3&updated=0&unchanged=0")
    assert again.status_code == 303
    assert again.headers["location"].endswith("added=0&updated=0&unchanged=3")
    assert rows == ["Reading log", "Short essay", "Textbook page 12"]
    assert (nothing.status_code, unwritten) == (200, [])
    assert nothing.text.count(f'<p class="empty">{NOTHING_SELECTED}</p>') == 1


def test_a_date_the_due_date_check_refuses_at_the_pages_today_is_no_choice(
    tmp_path: pathlib.Path,
) -> None:
    """A draft read again more than a year after the dates it read: a deadline as read and a
    weekday the note leaves open that the due-date check refuses at the page's today are said
    on the card with why they can't be the due date, never offered as a choice."""
    with client_in(tmp_path) as client:
        page = client.post("/parent/inbox/read", data={"text": FIELDS}).text
    with client_in(tmp_path, today="2027-11-10") as client:
        mapped, practice = key_of(page, "Map project"), key_of(page, "Online practice")
        shown = client.post("/parent/inbox/find", data=ticked_form(page) | {"recheck": "1"})
        map_open = opened(client, page, mapped)
        practice_open = opened(client, page, practice)

    far_map = (
        '<p class="source">The note names Wednesday, November 4, 2026. It can&#39;t be saved '
        "as the due date because it&#39;s more than a year ago.</p>"
    )
    far_tuesday = (
        '<p class="source">The note names Tuesday, November 3, 2026. It can&#39;t be saved '
        "as the due date because it&#39;s more than a year ago.</p>"
    )
    assert (shown.status_code, map_open.status_code, practice_open.status_code) == (200,) * 3
    assert card(shown.text, "Map project").count(far_map) == 1
    assert card(shown.text, "Online practice").count(far_tuesday) == 1
    map_card = card(map_open.text, "Map project")
    assert map_card.count(far_map) == 1
    assert when_options(map_card, mapped) == [("none", True), ("other", False)]
    practice_card = card(practice_open.text, "Online practice")
    assert practice_card.count(far_tuesday) == 1
    assert when_options(practice_card, practice) == [
        ("2026-11-10", False),
        ("none", True),
        ("other", False),
    ]


FAR_MAP = (
    '<p class="source">The note names Wednesday, November 4, 2026. It can&#39;t be saved '
    "as the due date because it&#39;s more than a year ago.</p>"
)
"""The Map project card's line when FIELDS, read on 2026-11-03, is read again on 2027-11-10."""


def test_a_deadline_as_read_the_check_refuses_at_the_pages_today_is_no_due_date_yet(
    tmp_path: pathlib.Path,
) -> None:
    """A draft read again more than a year after the deadline it read: the save-ready card
    says "No due date yet" with the deadline as read shown as the note's, with why it can't
    be the due date; Map project ticked and saved, it is written with no due date, no claim
    and no origin for one."""
    with client_in(tmp_path) as client:
        page = client.post("/parent/inbox/read", data={"text": FIELDS}).text
    with client_in(tmp_path, today="2027-11-10") as client:
        mapped = key_of(page, "Map project")
        shown = client.post("/parent/inbox/find", data=ticked_form(page) | {"recheck": "1"})
        kept = client.post(
            "/parent/inbox/keep", data=ticked_form(shown.text) | {f"take-{mapped}": "1"}
        )
        rows = store_of(client).all_assignments()
        claims = [store_of(client).deadline_records(row.assignment_id) for row in rows]

    map_card = card(shown.text, "Map project")
    assert map_card.count(reviewed_line("Map project", "No due date yet")) == 1
    assert map_card.count(FAR_MAP) == 1
    assert kept.status_code == 303
    assert [(row.title, row.due_date, row.origins.get("due_date")) for row in rows] == [
        ("Map project", None, None)
    ]
    assert claims == [[]]


def test_a_refused_deadline_as_read_leaves_the_family_a_valid_date_to_enter(
    tmp_path: pathlib.Path,
) -> None:
    """The same draft read again: Map project ticked with another date within a year typed,
    reviewed and saved, is written with that date as the family's entry."""
    with client_in(tmp_path) as client:
        page = client.post("/parent/inbox/read", data={"text": FIELDS}).text
    with client_in(tmp_path, today="2027-11-10") as client:
        mapped = key_of(page, "Map project")
        form = ticked_form(page) | {
            f"take-{mapped}": "1",
            f"when-{mapped}": "other",
            f"when_other-{mapped}": "2027-11-20",
            "recheck": "1",
        }
        reviewed = client.post("/parent/inbox/find", data=form)
        kept = client.post("/parent/inbox/keep", data=ticked_form(reviewed.text))
        rows = store_of(client).all_assignments()

    assert reviewed.status_code == 200
    assert card(reviewed.text, "Map project").count(FAR_MAP) == 1
    assert kept.status_code == 303
    assert [(row.title, row.due_date, row.origins["due_date"]) for row in rows] == [
        ("Map project", datetime.date(2027, 11, 20), SourceChannel.PARENT_ENTRY)
    ]


@pytest.mark.parametrize(
    ("typed", "due", "origin"),
    [
        (None, datetime.date(2026, 11, 4), SourceChannel.LMS),
        ("2026-11-20", datetime.date(2026, 11, 20), SourceChannel.PARENT_ENTRY),
    ],
    ids=["schools", "familys"],
)
def test_a_refused_deadline_as_read_never_clears_or_replaces_a_saved_due_date(
    tmp_path: pathlib.Path,
    typed: str | None,
    due: datetime.date,
    origin: SourceChannel,
) -> None:
    """Map project saved with the deadline as read or with the family's date, then the same
    draft read again more than a year later and saved with Map project ticked: the saved
    due date and its origin stay as they were."""
    with client_in(tmp_path) as client:
        page = client.post("/parent/inbox/read", data={"text": FIELDS}).text
        mapped = key_of(page, "Map project")
        first = ticked_form(page) | {f"take-{mapped}": "1"}
        if typed is not None:
            first |= {f"when-{mapped}": "other", f"when_other-{mapped}": typed, "recheck": "1"}
            first = ticked_form(client.post("/parent/inbox/find", data=first).text)
        saved = client.post("/parent/inbox/keep", data=first)
    with client_in(tmp_path, today="2027-11-10") as client:
        shown = client.post("/parent/inbox/find", data=ticked_form(page) | {"recheck": "1"})
        kept = client.post(
            "/parent/inbox/keep", data=ticked_form(shown.text) | {f"take-{mapped}": "1"}
        )
        rows = store_of(client).all_assignments()

    assert saved.status_code == 303
    assert kept.status_code == 303
    assert [(row.title, row.due_date, row.origins.get("due_date")) for row in rows] == [
        ("Map project", due, origin)
    ]


def test_edit_details_then_review_changes_then_save_writes_the_edited_title_once(
    tmp_path: pathlib.Path,
) -> None:
    """Reading log ticked, Edit details pressed: the review comes back with that card's title
    box and due-date choices open, the tick kept, Review changes as the action and first
    press, no Save, and nothing written. The title typed and Review changes pressed: the
    save-ready review with the title applied and signed. Save then writes one row with it."""
    with client_in(tmp_path) as client:
        page = client.post("/parent/inbox/read", data={"text": NOTED}).text
        logged = key_of(page, "Reading log")
        editing = opened(client, page, logged, **{f"take-{logged}": "1"})
        unwritten = store_of(client).all_assignments()
        typed = ticked_form(editing.text) | {f"title-{logged}": "Edited review title"}
        reviewed = client.post("/parent/inbox/find", data=typed | {"recheck": "1"})
        kept = client.post("/parent/inbox/keep", data=ticked_form(reviewed.text))
        rows = sorted(row.title for row in store_of(client).all_assignments())

    assert (editing.status_code, unwritten) == (200, [])
    body = card(editing.text, "Reading log")
    assert f'<input type="text" name="title-{logged}" value="Reading log"' in body
    assert when_options(body, logged) == [("2026-11-02", True), ("none", False), ("other", False)]
    assert f'name="details" value="{logged}"' not in body
    assert logged in numbers(editing.text, TICKED)
    assert SAVE_SELECTED not in editing.text
    assert editing.text.count(REVIEW_CHANGES) == 1
    assert first_press(editing.text) == DEFAULT_PRESS
    assert reviewed.status_code == 200
    assert 'type="text" name="title-' not in reviewed.text
    assert reviewed.text.count(SAVE_SELECTED) == 1
    assert signed_edits(reviewed.text)[logged] == ["Edited review title", "2026-11-02"]
    assert reviewed_line("Edited review title", "Monday, November 2, 2026") in card(
        reviewed.text, "Edited review title"
    )
    assert kept.status_code == 303
    assert kept.headers["location"].endswith("added=3&updated=0&unchanged=0")
    assert rows == ["Edited review title", "Short essay", "Textbook page 12"]


def test_edit_details_then_review_changes_then_save_writes_the_familys_date_once(
    tmp_path: pathlib.Path,
) -> None:
    """Another date chosen in the opened details of a selected note, reviewed and saved: one
    press writes the date as the family's entry."""
    with client_in(tmp_path) as client:
        page = homework_selected(client, FIELDS)
        mapped = key_of(page, "Map project")
        editing = opened(client, page, mapped)
        typed = ticked_form(editing.text) | {
            f"when-{mapped}": "other",
            f"when_other-{mapped}": "2026-11-09",
        }
        reviewed = client.post("/parent/inbox/find", data=typed | {"recheck": "1"})
        kept = client.post("/parent/inbox/keep", data=ticked_form(reviewed.text))
        rows = {row.title: row for row in store_of(client).all_assignments()}

    assert reviewed.status_code == 200
    assert reviewed_line("Map project", "Monday, November 9, 2026") in card(
        reviewed.text, "Map project"
    )
    assert kept.status_code == 303
    assert (rows["Map project"].due_date, rows["Map project"].origins["due_date"]) == (
        datetime.date(2026, 11, 9),
        SourceChannel.PARENT_ENTRY,
    )


def test_a_refused_date_keeps_the_details_open_with_the_typing(tmp_path: pathlib.Path) -> None:
    """Review changes with another date that names no date: refused, nothing written, and the
    card's details stay open with the date and title as typed and no Save offered."""
    with client_in(tmp_path) as client:
        page = homework_selected(client, FIELDS)
        mapped = key_of(page, "Map project")
        editing = opened(client, page, mapped)
        typed = ticked_form(editing.text) | {
            f"title-{mapped}": "Map of the river",
            f"when-{mapped}": "other",
            f"when_other-{mapped}": "soon",
        }
        refused = client.post("/parent/inbox/find", data=typed | {"recheck": "1"})
        rows = store_of(client).all_assignments()

    assert (refused.status_code, rows) == (422, [])
    body = card(refused.text, "Map of the river")
    assert f'<input type="text" name="title-{mapped}" value="Map of the river"' in body
    assert f'<input type="date" name="when_other-{mapped}" value="soon"' in body
    assert ("other", True) in when_options(body, mapped)
    assert SAVE_SELECTED not in refused.text
    assert refused.text.count(REVIEW_CHANGES) == 1


def test_a_save_with_details_no_review_showed_writes_nothing_and_opens_them(
    tmp_path: pathlib.Path,
) -> None:
    """A Save carrying a title the save-ready review never showed (a hand-made request), and
    a Save sent from a review with the details open (which offers no Save): nothing written,
    the details concerned open with the typing, and the summary says why. Review changes then
    Save writes the title once."""
    with client_in(tmp_path) as client:
        page = homework_selected(client, FIELDS)
        mapped = key_of(page, "Map project")
        handmade = client.post(
            "/parent/inbox/keep", data=ticked_form(page) | {f"title-{mapped}": "Map of the river"}
        )
        editing = opened(client, page, mapped)
        from_editing = client.post("/parent/inbox/keep", data=ticked_form(editing.text))
        unwritten = store_of(client).all_assignments()
        reviewed = client.post(
            "/parent/inbox/find", data=ticked_form(handmade.text) | {"recheck": "1"}
        )
        kept = client.post("/parent/inbox/keep", data=ticked_form(reviewed.text))
        rows = sorted(row.title for row in store_of(client).all_assignments())

    assert (handmade.status_code, from_editing.status_code, unwritten) == (200, 200, [])
    link = f'<a href="#card-{mapped}">Check this assignment</a></p>'
    for answer, title in ((handmade, "Map of the river"), (from_editing, "Map project")):
        assert f"{SUMMARY}{EDITS_IN_REVIEW} {link}" in answer.text
        body = card(answer.text, title)
        assert f'<input type="text" name="title-{mapped}" value="{title}"' in body
        assert SAVE_SELECTED not in answer.text
        assert answer.text.count(REVIEW_CHANGES) == 1
    assert kept.status_code == 303
    assert "Map of the river" in rows
    assert "Map project" not in rows


TWICE_LINE = (
    '<p class="problem">A choice on this card was sent twice with different answers. '
    "Choose it once.</p>"
)
"""What a card says when a choice on it was sent twice with different answers."""

BOXES_TWICE = [
    pytest.param("title", ("Reading log one", "Reading log two"), id="title"),
    pytest.param("title", ("Reading log two", "Reading log one"), id="title-reversed"),
    pytest.param("when", ("2026-11-02", "none"), id="when"),
    pytest.param("when", ("none", "2026-11-02"), id="when-reversed"),
    pytest.param("when_other", ("2026-11-05", "2026-11-06"), id="when-other"),
    pytest.param("when_other", ("2026-11-06", "2026-11-05"), id="when-other-reversed"),
]
"""A note's title box, due-date choice and date box, each sent twice with two different
answers, in both orders."""


@pytest.mark.parametrize("path", ["keep", "find"])
@pytest.mark.parametrize(("head", "values"), BOXES_TWICE)
def test_a_note_box_sent_twice_with_different_answers_saves_nothing_and_names_its_card(
    tmp_path: pathlib.Path, path: str, head: str, values: tuple[str, str]
) -> None:
    """Save or Review changes with a note's title box, due-date choice or date box sent twice
    with different answers: refused, nothing written, the summary says why with a link to the
    card, the card says so with its details open, and the other tick and typed title come
    back as sent."""
    with client_in(tmp_path) as client:
        page = client.post("/parent/inbox/read", data={"text": NOTED}).text
        logged, mapped = key_of(page, "Reading log"), key_of(page, "Map project")
        others = ticked_form(page) | {
            f"take-{logged}": "1",
            f"take-{mapped}": "1",
            f"title-{mapped}": "Map project outline",
            f"editing-{mapped}": "1",
        }
        if path == "find":
            others["recheck"] = "1"
        fields = [*others.items(), *((f"{head}-{logged}", value) for value in values)]
        refused = sent_as_pairs(client, path, fields)
        counts = record_counts(client)

    assert refused.status_code == 422
    assert counts == (0, 0, 0)
    assert f"{SUMMARY}{SENT_TWICE} {CHECK_CARD.format(key=logged)}" in refused.text
    body = card(refused.text, "Reading log")
    assert unescaped(body).count(twice_said(head, values)) == 1
    assert f'<input type="text" name="title-{logged}"' in body
    assert numbers(refused.text, TICKED) == {logged, mapped}
    assert f'<input type="text" name="title-{mapped}" value="Map project outline"' in refused.text
    assert SAVE_SELECTED not in refused.text


@pytest.mark.parametrize("path", ["keep", "find"])
@pytest.mark.parametrize("reverse", [False, True], ids=["in-order", "reversed"])
def test_choices_sent_twice_on_several_cards_name_each_card_and_save_nothing(
    tmp_path: pathlib.Path, path: str, reverse: bool
) -> None:
    """Save or Review changes with a tick, a joined note's tick and a title box each sent
    twice with different answers, on three cards: refused whole, nothing written, and each
    card concerned says so; the summary links to none of them."""
    with client_in(tmp_path) as client:
        page = client.post("/parent/inbox/read", data={"text": NOTED}).text
        logged, mapped = key_of(page, "Reading log"), key_of(page, "Map project")
        essay = key_of_card(page, "Short essay")
        twice = [
            (f"take-{logged}", ("1", "0")),
            (f"join-{essay}-0", ("1", "0")),
            (f"title-{mapped}", ("Map one", "Map two")),
        ]
        others = ticked_form(page) | ({"recheck": "1"} if path == "find" else {})
        fields = [
            *others.items(),
            *(
                (name, value)
                for name, values in twice
                for value in (reversed(values) if reverse else values)
            ),
        ]
        refused = sent_as_pairs(client, path, fields)
        counts = record_counts(client)

    assert refused.status_code == 422
    assert counts == (0, 0, 0)
    assert f"{SUMMARY}{SENT_TWICE}</p>" in refused.text
    titled = ("Map two", "Map one") if reverse else ("Map one", "Map two")
    for title, said in (
        ("Reading log", TICK_TWICE),
        ("Map project", twice_said("title", titled)),
        ("Short essay", JOIN_TWICE),
    ):
        assert unescaped(card(refused.text, title)).count(said) == 1
    assert TWICE_LINE not in refused.text


def key_of_card(page: str, title: str) -> int:
    """The key of a card read from no note, as its kind field names it."""
    found = re.search(r'name="kind-(\d+)"', card(page, title))
    assert found is not None, title
    return int(found.group(1))


CARD_FIELDS_TWICE = [
    pytest.param("occurrence-{key}", ("update", "new"), id="occurrence-not-asked"),
    pytest.param("suggested-{key}", ("HOMEWORK", "TASK"), id="suggested"),
    pytest.param("shown-{key}", ("HOMEWORK", "TASK"), id="shown"),
    pytest.param("chosen-{key}", ("1", "2"), id="chosen"),
    pytest.param("folded-{key}", ("1", "2"), id="folded"),
    pytest.param("asked-{key}", ("1", "2"), id="asked"),
    pytest.param("which-{key}", ("1", "2"), id="which"),
    pytest.param("editing-{key}", ("1", "2"), id="editing"),
    pytest.param("unsaved_why-{key}", ("stale", "differ"), id="unsaved-why"),
    pytest.param("unsaved-{key}-0-0", ("Use ink.", "Use pencil."), id="unsaved"),
    pytest.param("unsaved_row-{key}-0-0", ("1", "2"), id="unsaved-row"),
    pytest.param("unsaved_unshown-{key}-0", ("1", "2"), id="unsaved-unshown"),
    pytest.param("unsaved_none-{key}-0", ("1", "2"), id="unsaved-none"),
    pytest.param("offered-{note}", ("1", "0"), id="offered"),
    pytest.param("unresolved-take-{note}", ('["1", "0"]', '["0"]'), id="unresolved-mark"),
    pytest.param("kind-{note}", ("HOMEWORK", "TASK"), id="kind-on-a-card-with-no-select"),
]
"""Each page field the review posts once for a card, injected twice with two different
answers: the technical fields of a card, a note's offered mark, a mark of a field sent twice,
and a card's type or which-it-is answer where the page shows no control for it (a note not
selected has no type; a card that asks nothing has no question of which it is)."""


@pytest.mark.parametrize("path", ["keep", "find"])
@pytest.mark.parametrize("reverse", [False, True], ids=["in-order", "reversed"])
@pytest.mark.parametrize(("field", "values"), CARD_FIELDS_TWICE)
def test_a_cards_page_field_sent_twice_with_different_answers_is_refused_whole(
    tmp_path: pathlib.Path, path: str, reverse: bool, field: str, values: tuple[str, str]
) -> None:
    """Save or Review changes with one of a card's page fields sent twice with different
    answers, in either order: the whole form is refused as a review that could not be
    verified, nothing is written, and no card or field is named."""
    with client_in(tmp_path) as client:
        page = client.post("/parent/inbox/read", data={"text": NOTED}).text
        essay = key_of_card(page, "Short essay")
        name = field.format(key=essay, note=key_of(page, "Reading log"))
        form = ticked_form(page) | ({"recheck": "1"} if path == "find" else {})
        form.pop(name, None)
        fields = [
            *form.items(),
            *((name, value) for value in (values[::-1] if reverse else values)),
        ]
        refused = sent_as_pairs(client, path, fields)
        counts = record_counts(client)

    assert refused.status_code == 409
    assert counts == (0, 0, 0)
    assert f"{SUMMARY}{COULD_NOT_BE_VERIFIED}</p>" in refused.text
    if field.startswith("unresolved"):
        # A mark only forces an answer: the field it names stays offered with nothing chosen.
        assert unresolved_control(refused.text, name.removeprefix("unresolved-"))
    else:
        assert "sent twice" not in refused.text
    assert 'id="card-' not in refused.text


KIND_TWICE = "The type was sent twice with different answers. Choose one."
SEARCH_TWICE = "The search words were sent twice with different answers. Type them again."
OCCURRENCE_TWICE = "The answer to which it is was sent twice with different answers. Choose one."
IDENTITY_TWICE = (
    "The answer to which homework this is was sent twice with different answers. Choose one."
)
"""What a card's type, search words, answer to which it is, and answer to which homework it
is say at their field when sent twice with different answers."""


@pytest.mark.parametrize("path", ["keep", "find"])
@pytest.mark.parametrize("reverse", [False, True], ids=["in-order", "reversed"])
@pytest.mark.parametrize(
    ("head", "values"),
    [("kind", ("HOMEWORK", "TASK")), ("search", ("essay", "quiz"))],
    ids=["type", "search"],
)
def test_a_cards_type_or_search_words_sent_twice_say_so_at_that_field(
    tmp_path: pathlib.Path, path: str, reverse: bool, head: str, values: tuple[str, str]
) -> None:
    """Save or Review changes with a card's type or its search words sent twice with
    different answers, in either order: nothing is written, the summary says why with a link
    to the card, and the line sits at that field, never above the card's fields; the search
    box comes back empty."""
    with client_in(tmp_path) as client:
        page = client.post("/parent/inbox/read", data={"text": NOTED}).text
        essay = key_of_card(page, "Short essay")
        name = f"{head}-{essay}"
        form = ticked_form(page) | ({"recheck": "1"} if path == "find" else {})
        form.pop(name, None)
        fields = [
            *form.items(),
            *((name, value) for value in (values[::-1] if reverse else values)),
        ]
        refused = sent_as_pairs(client, path, fields)
        counts = record_counts(client)

    body = card(refused.text, "Short essay")
    assert refused.status_code == 422
    assert counts == (0, 0, 0)
    assert f"{SUMMARY}{SENT_TWICE} {CHECK_CARD.format(key=essay)}" in refused.text
    assert TWICE_LINE not in body
    if head == "kind":
        label = re.search(r'<label class="type-choice">.*?</label>', body, re.S)
        assert label is not None
        assert f'<span class="problem">{KIND_TWICE}</span>' in label.group(0)
    else:
        rename = re.search(r'<details class="rename".*?</details>', body, re.S)
        assert rename is not None
        assert f'<p class="problem">{SEARCH_TWICE}</p>' in rename.group(0)
        assert re.search(rf'name="search-{essay}" maxlength="\d+" value=""', rename.group(0))


PAGE_FIELDS_TWICE = [
    pytest.param("text", NOTED.replace("Reading log", "Reading list"), id="text"),
    pytest.param("made_with", "0" * 16, id="made-with"),
    pytest.param("read_on", "2026-11-04", id="read-on"),
    pytest.param("read_at", "2026-11-04T09:00:00-05:00", id="read-at"),
]
"""Each single-answer field the review posts once for the whole page, with another answer
beside the one the page wrote."""


@pytest.mark.parametrize("reverse", [False, True], ids=["page-first", "page-last"])
@pytest.mark.parametrize(("name", "other"), PAGE_FIELDS_TWICE)
def test_a_page_field_sent_twice_with_different_answers_takes_neither_and_saves_nothing(
    tmp_path: pathlib.Path, reverse: bool, name: str, other: str
) -> None:
    """Save with the pasted text, the page's signature or the day or moment the text was read
    sent twice with different answers, in either order, beside a note ticked: neither answer
    is taken, and nothing is written."""
    with client_in(tmp_path) as client:
        page = client.post("/parent/inbox/read", data={"text": NOTED}).text
        form = ticked_form(page) | {f"take-{key_of(page, 'Reading log')}": "1"}
        values = (form.pop(name), other)
        fields = [
            *form.items(),
            *((name, value) for value in (values[::-1] if reverse else values)),
        ]
        answer = sent_as_pairs(client, "keep", fields)
        counts = record_counts(client)

    assert answer.status_code != 303
    assert counts == (0, 0, 0)


TICK_TWICE = 'The choice "Save this as homework" was sent twice with different answers. Choose one.'
JOIN_TWICE = (
    'The choice "Add this note to the assignment" was sent twice with different answers. '
    "Choose one."
)
"""What a note's tick and a joined note's tick sent twice with different answers say beside
them."""


def twice_said(field: str, values: tuple[str, ...]) -> str:
    """What the title box or the due date of a note sent twice with different answers says,
    naming each value in the order sent."""
    if field == "title":
        return (
            "The title was sent twice with different answers: "
            + " and ".join(f'"{value}"' for value in values)
            + ". Type the title."
        )
    words = {
        "2026-11-02": "Monday, November 2, 2026",
        "2026-11-05": "Thursday, November 5, 2026",
        "2026-11-06": "Friday, November 6, 2026",
        "none": "No due date yet",
    }
    which = "The due date" if field == "when" else "The date beside Another date"
    named = " and ".join(words[value] for value in values)
    return f"{which} was sent twice with different answers: {named}. Choose one."


def unescaped(body: str) -> str:
    """A page's markup with its character references read, so words can be found as said."""
    return html.unescape(body)


@pytest.mark.parametrize("path", ["keep", "find"])
@pytest.mark.parametrize(("name", "values", "key"), TWICE)
def test_a_tick_sent_twice_says_so_beside_that_tick(
    tmp_path: pathlib.Path, path: str, name: str, values: tuple[str, str], key: int
) -> None:
    """Save or Review changes with a note's tick or a joined note's tick sent twice with
    different answers: the line naming that choice sits beside that tick, in its own block,
    and the card says nothing else of it."""
    with client_in(tmp_path) as client:
        page = client.post("/parent/inbox/read", data={"text": NOTED}).text
        others = ticked_form(page) | ({"recheck": "1"} if path == "find" else {})
        fields = [*others.items(), *((name, value) for value in values)]
        refused = sent_as_pairs(client, path, fields)

    assert refused.status_code == 422
    shown = unescaped(refused.text)
    said = TICK_TWICE if name.startswith("take") else JOIN_TWICE
    label = "Save this as homework" if name.startswith("take") else ADD_THIS_NOTE
    block = re.search(
        rf'<div class="choice">\s*<label>[^\n]*name="{name}"[^\n]*{label}.*?</label>\s*'
        r"(?:<label>[^\n]*\n\s*)?"
        rf'<p class="problem">{re.escape(said)}</p>\s*</div>',
        shown,
    )
    assert block is not None
    assert shown.count(said) == 1
    assert TWICE_LINE not in refused.text


@pytest.mark.parametrize("path", ["keep", "find"])
@pytest.mark.parametrize(("head", "values"), BOXES_TWICE)
def test_a_note_box_sent_twice_says_so_at_that_field_with_each_value(
    tmp_path: pathlib.Path, path: str, head: str, values: tuple[str, str]
) -> None:
    """Save or Review changes with a note's title box, due-date choice or date box sent twice
    with different answers: the line naming that field and each value sent sits in the
    title's label or in the due date's fieldset, and the card says nothing else of it."""
    with client_in(tmp_path) as client:
        page = client.post("/parent/inbox/read", data={"text": NOTED}).text
        logged = key_of(page, "Reading log")
        others = ticked_form(page) | {f"take-{logged}": "1"}
        if path == "find":
            others["recheck"] = "1"
        fields = [*others.items(), *((f"{head}-{logged}", value) for value in values)]
        refused = sent_as_pairs(client, path, fields)

    assert refused.status_code == 422
    body = unescaped(card(refused.text, "Reading log"))
    said = twice_said(head, values)
    assert body.count(said) == 1
    if head == "title":
        assert re.search(
            rf"<label>Title<span[^>]*>[^<]*</span> <span class=\"problem\">"
            rf"{re.escape(said)}</span> <input type=\"text\" name=\"title-{logged}\"",
            body,
        )
    else:
        assert re.search(
            rf'<fieldset class="choice" id="when-{logged}"[^>]*>\s*<legend>.*?</legend>\s*'
            rf'<p class="problem">{re.escape(said)}</p>',
            body,
            re.S,
        )
    assert TWICE_LINE not in refused.text


def test_several_fields_sent_twice_on_one_card_each_get_their_line(tmp_path: pathlib.Path) -> None:
    """A note's tick, title box, due-date choice and date box all sent twice with different
    answers on one card: each field says so at that field, once."""
    with client_in(tmp_path) as client:
        page = client.post("/parent/inbox/read", data={"text": NOTED}).text
        logged = key_of(page, "Reading log")
        twice = [
            (f"take-{logged}", ("1", "0")),
            (f"title-{logged}", ("Reading log one", "Reading log two")),
            (f"when-{logged}", ("2026-11-02", "none")),
            (f"when_other-{logged}", ("2026-11-05", "2026-11-06")),
        ]
        fields = [
            *ticked_form(page).items(),
            *((name, value) for name, values in twice for value in values),
        ]
        refused = sent_as_pairs(client, "keep", fields)
        counts = record_counts(client)

    assert (refused.status_code, counts) == (422, (0, 0, 0))
    body = unescaped(card(refused.text, "Reading log"))
    for said in (
        TICK_TWICE,
        twice_said("title", ("Reading log one", "Reading log two")),
        twice_said("when", ("2026-11-02", "none")),
        twice_said("when_other", ("2026-11-05", "2026-11-06")),
    ):
        assert body.count(said) == 1, said
    assert body.index(twice_said("when", ("2026-11-02", "none"))) < body.index(
        twice_said("when_other", ("2026-11-05", "2026-11-06"))
    )


UNANSWERED_TWICE = (
    "A choice sent twice with different answers needs your answer, so nothing was saved. "
    "Choose it, then save."
)
"""What the review says when a choice sent twice is sent back with no answer."""

RECOVERY = [
    pytest.param("take", ("1", "0"), id="tick"),
    pytest.param("take", ("0", "1"), id="tick-reversed"),
    pytest.param("join", ("1", "0"), id="joined-tick"),
    pytest.param("join", ("0", "1"), id="joined-tick-reversed"),
    pytest.param("title", ("Reading log one", "Reading log two"), id="title"),
    pytest.param("title", ("Reading log two", "Reading log one"), id="title-reversed"),
    pytest.param("when", ("2026-11-02", "none"), id="due-date"),
    pytest.param("when", ("none", "2026-11-02"), id="due-date-reversed"),
]
"""Each kind of note field sent twice with different answers, in both orders."""


def unresolved_control(body: str, field: str) -> bool:
    """Whether a card offers the field's actual choice with nothing chosen for the family: two
    unchecked required choices for a tick, an empty title box, or every due-date
    choice unchecked with no due date among them."""
    head, _, key = field.partition("-")
    if head in ("take", "join"):
        said = (
            ("Save this as homework", "Leave it out")
            if head == "take"
            else (ADD_THIS_NOTE, "Don't add it")
        )
        return (
            all(
                re.search(
                    rf'<label><input type="radio" name="{field}" value="{value}" required> '
                    rf"<span>{words}<",
                    body,
                )
                for value, words in zip(("1", "0"), said, strict=True)
            )
            and f'name="{field}" value="1" checked' not in body
        )
    if head == "title":
        return f'<input type="text" name="{field}" value="" ' in body
    options = when_options(body, int(key))
    return ("none", False) in options and not any(chosen for _, chosen in options)


@pytest.mark.parametrize(("head", "values"), RECOVERY)
def test_a_choice_sent_twice_stays_unanswered_until_the_family_answers_it(
    tmp_path: pathlib.Path, head: str, values: tuple[str, str]
) -> None:
    """A note's tick, a joined note's tick, a title box or a due date sent twice with
    different answers: refused with that field named and its actual choice offered with
    nothing chosen; the returned form sent back unchanged is refused again with zero writes
    and the field named again, every other edit kept; the explicit answer then gives the
    ordinary review on Review changes, and Save writes once with that answer."""
    with client_in(tmp_path) as client:
        page = client.post("/parent/inbox/read", data={"text": NOTED}).text
        logged, mapped = key_of(page, "Reading log"), key_of(page, "Map project")
        essay = key_of_card(page, "Short essay")
        field = f"join-{essay}-0" if head == "join" else f"{head}-{logged}"
        others = ticked_form(page) | {f"take-{mapped}": "1"}
        if head in ("title", "when"):
            others[f"take-{logged}"] = "1"
        conflict = sent_as_pairs(
            client, "keep", [*others.items(), *((field, value) for value in values)]
        )
        retry = client.post("/parent/inbox/keep", data=ticked_form(conflict.text))
        retried_find = client.post(
            "/parent/inbox/find", data=ticked_form(conflict.text) | {"recheck": "1"}
        )
        unwritten = record_counts(client)
        answer = {
            "take": "1",
            "join": "1",
            "title": "Reading log answered",
            "when": "none",
        }[head]
        reviewed = client.post(
            "/parent/inbox/find",
            data=ticked_form(retry.text) | {field: answer, "recheck": "1"},
        )
        kept = client.post("/parent/inbox/keep", data=ticked_form(reviewed.text))
        again = client.post("/parent/inbox/keep", data=ticked_form(reviewed.text))
        rows = {row.title: row for row in store_of(client).all_assignments()}

    owner = essay if head == "join" else logged
    title = "Short essay" if head == "join" else "Reading log"
    said = {"take": TICK_TWICE, "join": JOIN_TWICE}.get(head) or twice_said(head, values)
    assert conflict.status_code == 422
    assert f"{SUMMARY}{SENT_TWICE} {CHECK_CARD.format(key=owner)}" in conflict.text
    for again_refused in (retry, retried_find):
        assert again_refused.status_code == 422
        notice = f"{SUMMARY}{UNANSWERED_TWICE} {CHECK_CARD.format(key=owner)}"
        assert notice in again_refused.text
    assert unwritten == (0, 0, 0)
    for refused in (conflict, retry, retried_find):
        body = card(refused.text, title)
        assert unescaped(body).count(said) == 1
        assert unresolved_control(body, field)
        assert f'name="unresolved-{field}"' in body
        assert mapped in numbers(refused.text, TICKED)
    assert reviewed.status_code == 200
    assert "unresolved-" not in reviewed.text
    assert said not in unescaped(reviewed.text)
    assert reviewed.text.count(SAVE_SELECTED) == 1
    assert kept.status_code == 303
    added = 3 if head == "join" else 4
    assert kept.headers["location"].endswith(f"added={added}&updated=0&unchanged=0")
    assert again.headers["location"].endswith(f"added=0&updated=0&unchanged={added}")
    if head == "join":
        assert joined_ticks(card(reviewed.text, "Short essay")) == {field: True}
    if head == "title":
        assert "Reading log answered" in rows
    if head == "when":
        assert rows["Reading log"].due_date is None
    if head == "take":
        assert "Reading log" in rows


MARKED = [
    pytest.param("take", ("1", "0"), "1", id="tick"),
    pytest.param("title", ("Reading log one", "Reading log two"), "Reading log answered", id="box"),
    pytest.param("when", ("2026-11-02", "none"), "none", id="answer"),
]
"""A note's tick, its title box and its due-date choice, each sent twice, and an explicit answer
to each."""


def conflicted(client: TestClient, head: str, values: tuple[str, str]) -> tuple[str, str, Answer]:
    """The noted paste's review with Reading log's field of this kind sent twice with these
    values on Save, Map project ticked: the field, its card's title and the refusal."""
    page = client.post("/parent/inbox/read", data={"text": NOTED}).text
    logged, mapped = key_of(page, "Reading log"), key_of(page, "Map project")
    field = f"{head}-{logged}"
    others = ticked_form(page) | {f"take-{mapped}": "1"}
    if head != "take":
        others[f"take-{logged}"] = "1"
    sent = [*others.items(), *((field, value) for value in values)]
    return field, "Reading log", sent_as_pairs(client, "keep", sent)


@pytest.mark.parametrize("tamper", ["removed", "changed"])
@pytest.mark.parametrize(("head", "values", "answer"), MARKED)
def test_a_returned_review_whose_mark_does_not_match_its_page_is_refused_whole(
    tmp_path: pathlib.Path, head: str, values: tuple[str, str], answer: str, tamper: str
) -> None:
    """A choice sent twice comes back marked, the mark signed with the page; the returned form
    with that mark removed, or holding other values, is not the page that was signed: Save and
    Review changes are refused whole as a review that could not be verified, nothing written,
    the field marked again. The explicit answer on the returned page then saves once."""
    with client_in(tmp_path) as client:
        field, title, conflict = conflicted(client, head, values)
        returned = ticked_form(conflict.text)
        mark = f"unresolved-{field}"
        present = mark in returned
        if tamper == "removed":
            returned.pop(mark, None)
        else:
            returned[mark] = json.dumps(["Another answer"])
        refused = client.post("/parent/inbox/keep", data=returned)
        refused_find = client.post("/parent/inbox/find", data=returned | {"recheck": "1"})
        unwritten = record_counts(client)
        reviewed = client.post(
            "/parent/inbox/find",
            data=ticked_form(conflict.text) | {field: answer, "recheck": "1"},
        )
        kept = client.post("/parent/inbox/keep", data=ticked_form(reviewed.text))
        rows = {row.title: row for row in store_of(client).all_assignments()}

    assert conflict.status_code == 422
    assert present
    for again in (refused, refused_find):
        assert again.status_code == 409
        assert f"{SUMMARY}{COULD_NOT_BE_VERIFIED}</p>" in again.text
        assert f'name="{mark}"' in card(again.text, title)
    assert unwritten == (0, 0, 0)
    assert reviewed.status_code == 200
    assert "unresolved-" not in reviewed.text
    assert kept.status_code == 303
    assert kept.headers["location"].endswith("added=4&updated=0&unchanged=0")
    if head == "title":
        assert "Reading log answered" in rows
    if head == "when":
        assert rows["Reading log"].due_date is None


@pytest.mark.parametrize(("head", "values", "answer"), MARKED)
def test_a_choice_sent_twice_stays_unanswered_across_a_restart_until_answered(
    tmp_path: pathlib.Path, head: str, values: tuple[str, str], answer: str
) -> None:
    """A choice sent twice comes back marked; the application starts again, so the returned
    page cannot be verified, and its form sent back unchanged keeps the mark as posted:
    refused, nothing written, the field marked again. The explicit answer then saves once."""
    with client_in(tmp_path) as client:
        field, title, conflict = conflicted(client, head, values)
    with client_in(tmp_path) as client:
        retry = client.post("/parent/inbox/keep", data=ticked_form(conflict.text))
        unwritten = record_counts(client)
        reviewed = client.post(
            "/parent/inbox/find",
            data=ticked_form(retry.text) | {field: answer, "recheck": "1"},
        )
        kept = client.post("/parent/inbox/keep", data=ticked_form(reviewed.text))
        again = client.post("/parent/inbox/keep", data=ticked_form(reviewed.text))

    assert conflict.status_code == 422
    assert retry.status_code in (409, 422)
    assert f'name="unresolved-{field}"' in card(retry.text, title)
    assert unwritten == (0, 0, 0)
    assert reviewed.status_code == 200
    assert "unresolved-" not in reviewed.text
    assert kept.status_code == 303
    assert kept.headers["location"].endswith("added=4&updated=0&unchanged=0")
    assert again.headers["location"].endswith("added=0&updated=0&unchanged=4")


def unresolved_kind(body: str, key: int) -> bool:
    """Whether a card's type comes back as a required choice with its empty first option
    selected and neither Homework nor Task chosen."""
    found = re.search(rf'<select name="kind-{key}"([^>]*)>(.*?)</select>', body, re.S)
    assert found is not None
    attributes, options = found.groups()
    return (
        " required" in attributes
        and re.match(r'\s*<option value="" selected>', options) is not None
        and options.count(" selected>") == 1
        and '<option value="HOMEWORK">' in options
        and '<option value="TASK">' in options
    )


@pytest.mark.parametrize("values", [("HOMEWORK", "TASK"), ("TASK", "HOMEWORK")], ids=str)
def test_a_type_sent_twice_stays_unanswered_until_the_family_chooses_one(
    tmp_path: pathlib.Path, values: tuple[str, str]
) -> None:
    """A card's Homework or Task choice sent twice with different answers, in either order:
    refused with the card named and the type offered with neither chosen; the returned form
    sent back unchanged is refused again with zero writes, through Save and Review changes;
    the explicit choice then gives the ordinary review, and Save writes once with it."""
    with client_in(tmp_path) as client:
        page = client.post("/parent/inbox/read", data={"text": NOTED}).text
        essay = key_of_card(page, "Short essay")
        field = f"kind-{essay}"
        form = ticked_form(page)
        form.pop(field)
        conflict = sent_as_pairs(
            client, "keep", [*form.items(), *((field, value) for value in values)]
        )
        retry = client.post("/parent/inbox/keep", data=ticked_form(conflict.text))
        retried_find = client.post(
            "/parent/inbox/find", data=ticked_form(conflict.text) | {"recheck": "1"}
        )
        unwritten = record_counts(client)
        reviewed = client.post(
            "/parent/inbox/find",
            data=ticked_form(retry.text) | {field: "TASK", "recheck": "1"},
        )
        kept = client.post("/parent/inbox/keep", data=ticked_form(reviewed.text))
        rows = {row.title: row for row in store_of(client).all_assignments()}

    assert conflict.status_code == 422
    assert f"{SUMMARY}{SENT_TWICE} {CHECK_CARD.format(key=essay)}" in conflict.text
    for again_refused in (retry, retried_find):
        assert again_refused.status_code == 422
        notice = f"{SUMMARY}{UNANSWERED_TWICE} {CHECK_CARD.format(key=essay)}"
        assert notice in again_refused.text
    assert unwritten == (0, 0, 0)
    for refused in (conflict, retry, retried_find):
        body = card(refused.text, "Short essay")
        assert unresolved_kind(body, essay)
        assert f'name="unresolved-{field}"' in body
        assert body.count(f'<span class="problem">{KIND_TWICE}</span>') == 1
        assert TWICE_LINE not in body
    assert reviewed.status_code == 200
    assert "unresolved-" not in reviewed.text
    assert reviewed.text.count(SAVE_SELECTED) == 1
    assert kept.status_code == 303
    assert rows["Short essay"].kind.value == "TASK"


@pytest.mark.parametrize("values", [("update", "new"), ("new", "update")], ids=str)
def test_a_same_or_new_answer_sent_twice_comes_back_unanswered_and_its_retry_writes_nothing(
    tmp_path: pathlib.Path, values: tuple[str, str]
) -> None:
    """The answer to whether a note is the same assignment or new work sent twice with
    different answers, in either order: refused with neither answer chosen; the returned form
    sent back unchanged writes nothing; the explicit answer then saves."""
    with client_in(tmp_path) as client:
        page = homework_selected(client, TWICE_NOTED)
        form = ticked_form(page)
        conflict = sent_as_pairs(
            client, "keep", [*form.items(), *(("occurrence-1", value) for value in values)]
        )
        retry = client.post("/parent/inbox/keep", data=ticked_form(conflict.text))
        unwritten = record_counts(client)
        kept = client.post(
            "/parent/inbox/keep", data=ticked_form(retry.text) | {"occurrence-1": "new"}
        )

    assert conflict.status_code == 422
    assert "<legend>Which is it?</legend>" in conflict.text
    for refused in (conflict, retry):
        assert not re.search(r'name="occurrence-1" value="[a-z]+" checked', refused.text)
        assert 'type="hidden" name="occurrence-1"' not in refused.text
    assert retry.status_code != 303
    assert unwritten == (0, 0, 0)
    assert kept.status_code == 303
    assert kept.headers["location"].endswith("added=2&updated=0&unchanged=0")


class ReviewControls(HTMLParser):
    """Every named control of a review's own form, the one posted to Save and to Review
    changes, with how the page renders it: ``hidden`` (a hidden input, or the hidden text),
    ``visible`` (any other input, a select, a text area shown), or ``press`` (a button)."""

    def __init__(self) -> None:
        super().__init__()
        self.inside = False
        self.found: list[tuple[str, str]] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        given = dict(attrs)
        if tag == "form":
            self.inside = given.get("action") == "/parent/inbox/keep"
            return
        name = given.get("name")
        if not self.inside or name is None:
            return
        if tag == "button":
            self.found.append((name, "press"))
        elif tag in ("input", "select", "textarea"):
            hidden = given.get("type") == "hidden" if tag == "input" else "hidden" in given
            self.found.append((name, "hidden" if hidden else "visible"))

    def handle_endtag(self, tag: str) -> None:
        if tag == "form":
            self.inside = False


def review_controls(page: str) -> list[tuple[str, str]]:
    """The named controls of a page's review form, each with how it is rendered."""
    parser = ReviewControls()
    parser.feed(page)
    parser.close()
    return parser.found


GUIDE_NOTE = "Get it signed at dinner."
GUIDE_CARD = (
    "Homework for Wren\n- 09/02/2026 - Wednesday\n"
    "Health - Assigned: Course Guide Due: (Due:09/09/2026)\n"
    "Return the course guide signed by a parent after reading it.\n"
)
"""A card whose class and title are those of homework she added from a note, which asks
which homework it is."""


def every_review_shape(tmp_path: pathlib.Path) -> dict[str, str]:
    """Each shape of the School text review this file builds, by name: the first view with
    and without text that needs review, a card's details open, the review after Review
    changes, refusals (a tick, a title, a type, the answer to which it is, the answer to
    which homework it is, a box to link her note, an instruction's box and the box that none
    applies sent twice), a folded card and that card shown again for its carried title sent
    twice, the question of which it is, the question of which homework it is, and search
    results."""
    shapes: dict[str, str] = {}
    with client_in(tmp_path / "notes") as client:
        first = client.post("/parent/inbox/read", data={"text": NOTED}).text
        shapes["first view with text that needs review"] = first
        shapes["first view"] = client.post(
            "/parent/inbox/read", data={"text": ONE_HOMEWORK_NOTE}
        ).text
        shapes["details open"] = opened(client, first, key_of(first, "Map project")).text
        shapes["after Review changes"] = homework_selected(client, NOTED)
        others = ticked_form(first)
        for name, values in (
            ("take-4", ("1", "0")),
            ("title-3", ("A", "B")),
            (f"kind-{key_of_card(first, 'Short essay')}", ("HOMEWORK", "TASK")),
        ):
            pairs = [*others.items(), *((name, value) for value in values)]
            shapes[f"refusal, {name} sent twice"] = sent_as_pairs(client, "keep", pairs).text
        shapes["refusal, a title no review showed"] = client.post(
            "/parent/inbox/keep", data=others | {"take-3": "1", "title-3": "Typed"}
        ).text
        asking = homework_selected(client, TWICE_NOTED)
        shapes["the question of which it is"] = asking
        shapes["a folded card"] = client.post(
            "/parent/inbox/find",
            data=ticked_form(asking) | {"occurrence-1": "update", "recheck": "1"},
        ).text
        shapes["refusal, occurrence-1 sent twice"] = sent_as_pairs(
            client,
            "keep",
            [*ticked_form(asking).items(), ("occurrence-1", "update"), ("occurrence-1", "new")],
        ).text
        folded = ticked_form(shapes["a folded card"])
        carried = folded.pop("title-1")
        shapes["a folded card shown again"] = sent_as_pairs(
            client,
            "keep",
            [*folded.items(), ("title-1", carried), ("title-1", "Another title")],
        ).text
    with client_in(tmp_path / "boxes") as client:
        asked, _ = box_page(client, "apply-1-0")
        for name in ("apply-1-0", "none-1"):
            pairs = [*returned_form(asked).items(), (name, "1"), (name, "0")]
            shapes[f"refusal, {name} sent twice"] = sent_as_pairs(client, "keep", pairs).text
    with client_in(tmp_path / "links", today="2026-09-03") as client:
        offered, _ = box_page(client, "link-0-0")
        pairs = [*returned_form(offered).items(), ("link-0-0", "1"), ("link-0-0", "0")]
        shapes["refusal, link-0-0 sent twice"] = sent_as_pairs(client, "keep", pairs).text
    with client_in(tmp_path / "search") as client:
        saved = client.post("/parent/inbox/read", data={"text": NO_NOTE}).text
        assert client.post("/parent/inbox/keep", data=ticked_form(saved)).status_code == 303
        page = client.post("/parent/inbox/read", data={"text": NOTED}).text
        found = client.post(
            "/parent/inbox/find",
            data=ticked_form(page) | {"search-0": "practice", "find": "0"},
        ).text
        shapes["search results"] = found
        target = re.search(r'name="choose" value="(0:[^"]+)"', found)
        assert target is not None
        shapes["a choice made from the results"] = client.post(
            "/parent/inbox/find", data=ticked_form(found) | {"choose": target.group(1)}
        ).text
    with client_in(tmp_path / "hers", today="2026-09-03") as client:
        homework_from_a_note(
            store_of(client),
            course="Health",
            title="Course Guide Due",
            due_date=datetime.date(2026, 9, 9),
            note=GUIDE_NOTE,
        )
        identified = client.post("/parent/inbox/read", data={"text": GUIDE_CARD}).text
        shapes["the question of which homework it is"] = identified
        answers = re.findall(r'name="identity-0" value="([^"]+)"', identified)
        shapes["refusal, identity-0 sent twice"] = sent_as_pairs(
            client,
            "keep",
            [*ticked_form(identified).items(), *(("identity-0", value) for value in answers)],
        ).text
    return shapes


def test_every_field_of_every_review_shape_is_in_the_review_forms_table(
    tmp_path: pathlib.Path,
) -> None:
    """Each shape of the School text review, rendered: every named control of its own form
    has an entry in the server's table of the review form's fields, by exact name and by how
    it is rendered, a press only as a press, a visible control only as a family answer. A
    name outside the table's grammar, or rendered other than its entry says, fails."""
    shapes = every_review_shape(tmp_path)
    seen: set[tuple[str, str]] = set()
    unknown: list[tuple[str, str, str]] = []
    for shape, page in shapes.items():
        controls = review_controls(page)
        assert controls, shape
        for name, rendering in controls:
            entry = review_field(name, rendering)
            if entry is None:
                unknown.append((shape, name, rendering))
                continue
            seen.add((entry.grammar, rendering))

    assert unknown == []
    assert 'id="needs-review"' in shapes["first view with text that needs review"]
    assert 'id="needs-review"' not in shapes["first view"]
    assert 'name="folded-1"' in shapes["a folded card"]
    assert 'id="occurrence-question-1"' in shapes["the question of which it is"]
    assert 'id="identity-question-0"' in shapes["the question of which homework it is"]
    assert 'name="choose"' in shapes["search results"]
    assert 'name="picked-0"' in shapes["a choice made from the results"]
    for mark in ("occurrence-1", "identity-0", "apply-1-0", "none-1", "link-0-0"):
        assert f'name="unresolved-{mark}"' in shapes[f"refusal, {mark} sent twice"], mark
    shown_again = shapes["a folded card shown again"]
    assert 'name="folded-1"' in shown_again
    assert 'name="unresolved-title-1"' in shown_again
    for entry in REVIEW_FIELDS:
        assert entry.role == {"press": "press", "visible": "answer"}.get(
            entry.rendering, entry.role
        ), entry
        assert entry.role in ("answer", "page", "press"), entry
        assert entry.shape == "one", entry
    assert {rendering for _, rendering in seen} == {"hidden", "visible", "press"}


@pytest.mark.parametrize(
    ("name", "rendering"),
    [
        ("take-01", "visible"),
        ("take-4x", "visible"),
        ("takes-4", "visible"),
        ("take-4-0", "visible"),
        ("take-1234567", "visible"),
        ("join-1", "visible"),
        ("suggested-0", "visible"),
        ("editing-0", "visible"),
        ("unresolved-take-4-1", "hidden"),
        ("unresolved-made_with", "hidden"),
        ("recheck", "visible"),
        ("details", "hidden"),
        ("made_with_x", "hidden"),
        ("link-1", "visible"),
    ],
)
def test_a_name_outside_the_grammar_or_rendered_otherwise_has_no_entry(
    name: str, rendering: str
) -> None:
    """The table matches whole names only, each by its own grammar and rendering: a padded
    number, a longer name, a name with a place it does not take, a page field shown as a
    control, a mark of no field, and a press as anything but a press have no entry."""
    assert review_field(name, rendering) is None


@pytest.mark.parametrize("reverse", [False, True], ids=["empty-first", "empty-last"])
def test_a_hidden_suggestion_injected_twice_on_a_notes_only_paste_is_refused_whole(
    tmp_path: pathlib.Path, reverse: bool
) -> None:
    """A notes-only paste whose form carries a card's suggested type, which that page never
    shows, injected twice with different answers: the whole form is refused as a review that
    could not be verified, nothing is written, and no card line points at a choice the page
    does not show."""
    with client_in(tmp_path) as client:
        page = client.post("/parent/inbox/read", data={"text": ONE_HOMEWORK_NOTE}).text
        values = ("", "x")[::-1] if reverse else ("", "x")
        fields = [*ticked_form(page).items(), *(("suggested-0", value) for value in values)]
        refused = sent_as_pairs(client, "keep", fields)
        counts = record_counts(client)

    assert refused.status_code == 409
    assert counts == (0, 0, 0)
    assert f"{SUMMARY}{COULD_NOT_BE_VERIFIED}</p>" in refused.text
    assert "sent twice" not in refused.text


def fieldset_of(page: str, ident: str) -> str:
    """The fieldset of a page with this id, from its opening tag to its end."""
    found = re.search(rf'<fieldset [^>]*id="{ident}".*?</fieldset>', page, re.S)
    assert found is not None, ident
    return found.group(0)


@pytest.mark.parametrize("reverse", [False, True], ids=["same-first", "different-first"])
def test_an_answer_to_which_homework_it_is_sent_twice_stays_unanswered_until_answered(
    tmp_path: pathlib.Path, reverse: bool
) -> None:
    """The answer to which homework a card is about sent twice with different answers, in
    either order: refused with nothing written and the line in that question, the question
    offered with nothing chosen and marked; the returned form sent back unchanged writes
    nothing; the explicit answer then saves. The same answer sent twice is one answer."""
    with client_in(tmp_path, today="2026-09-03") as client:
        homework_from_a_note(
            store_of(client),
            course="Health",
            title="Course Guide Due",
            due_date=datetime.date(2026, 9, 9),
            note=GUIDE_NOTE,
        )
        page = client.post("/parent/inbox/read", data={"text": GUIDE_CARD}).text
        mine, different = re.findall(r'name="identity-0" value="([^"]+)"', page)
        before = record_counts(client)
        values = (different, mine) if reverse else (mine, different)
        conflict = sent_as_pairs(
            client, "keep", [*ticked_form(page).items(), *(("identity-0", v) for v in values)]
        )
        retry = client.post("/parent/inbox/keep", data=ticked_form(conflict.text))
        unwritten = record_counts(client)
        kept = client.post(
            "/parent/inbox/keep", data=ticked_form(retry.text) | {"identity-0": mine}
        )
    with client_in(tmp_path / "again", today="2026-09-03") as client:
        homework_from_a_note(
            store_of(client),
            course="Health",
            title="Course Guide Due",
            due_date=datetime.date(2026, 9, 9),
            note=GUIDE_NOTE,
        )
        page = client.post("/parent/inbox/read", data={"text": GUIDE_CARD}).text
        hers = re.findall(r'name="identity-0" value="([^"]+)"', page)[0]
        once = sent_as_pairs(
            client, "keep", [*ticked_form(page).items(), ("identity-0", hers), ("identity-0", hers)]
        )

    assert conflict.status_code == 422
    assert f"{SUMMARY}{SENT_TWICE}" in conflict.text
    assert retry.status_code == 422
    assert f"{SUMMARY}{UNANSWERED_TWICE}" in retry.text
    for refused in (conflict, retry):
        asked = fieldset_of(refused.text, "identity-question-0")
        assert unescaped(asked).count(IDENTITY_TWICE) == 1
        assert 'name="unresolved-identity-0"' in asked
        assert "checked" not in asked
    assert unwritten == before
    assert kept.status_code == 303
    assert once.status_code == 303


@pytest.mark.parametrize("values", [("update", "new"), ("new", "update")], ids=str)
def test_an_answer_to_which_it_is_sent_twice_says_so_in_its_question_until_answered(
    tmp_path: pathlib.Path, values: tuple[str, str]
) -> None:
    """The answer to whether a note is the same assignment or new work sent twice with
    different answers, in either order: the line sits in that question, which is marked
    until answered; the unchanged retry writes nothing; the explicit answer saves."""
    with client_in(tmp_path) as client:
        page = homework_selected(client, TWICE_NOTED)
        conflict = sent_as_pairs(
            client,
            "keep",
            [*ticked_form(page).items(), *(("occurrence-1", value) for value in values)],
        )
        retry = client.post("/parent/inbox/keep", data=ticked_form(conflict.text))
        unwritten = record_counts(client)
        kept = client.post(
            "/parent/inbox/keep", data=ticked_form(retry.text) | {"occurrence-1": "new"}
        )

    assert conflict.status_code == 422
    assert retry.status_code == 422
    assert f"{SUMMARY}{UNANSWERED_TWICE}" in retry.text
    for refused in (conflict, retry):
        asked = fieldset_of(refused.text, "occurrence-question-1")
        assert unescaped(asked).count(OCCURRENCE_TWICE) == 1
        assert 'name="unresolved-occurrence-1"' in asked
        assert "checked" not in asked
    assert unwritten == (0, 0, 0)
    assert kept.status_code == 303


@pytest.mark.parametrize("reverse", [False, True], ids=["carried-first", "carried-last"])
@pytest.mark.parametrize("head", ["title", "when"])
def test_a_closed_cards_carried_answer_sent_twice_opens_it_and_stays_unanswered(
    tmp_path: pathlib.Path, head: str, reverse: bool
) -> None:
    """A note card closed after Review changes carries its reviewed title and due date
    hidden. One of them injected twice with different answers, in either order: nothing is
    written, the card's details open with the field named and marked; the returned form sent
    back unchanged writes nothing."""
    with client_in(tmp_path) as client:
        first = client.post("/parent/inbox/read", data={"text": NOTED}).text
        key = key_of(first, "Map project")
        opened_page = opened(client, first, key).text
        reviewed = client.post(
            "/parent/inbox/find",
            data=ticked_form(opened_page)
            | {f"take-{key}": "1", f"title-{key}": "Map outline", "recheck": "1"},
        ).text
        form = ticked_form(reviewed)
        name = f"{head}-{key}"
        carried = form.pop(name)
        values = (carried, "Other title" if head == "title" else "none")
        conflict = sent_as_pairs(
            client,
            "keep",
            [*form.items(), *((name, value) for value in (values[::-1] if reverse else values))],
        )
        retry = client.post("/parent/inbox/keep", data=ticked_form(conflict.text))
        counts = record_counts(client)

    assert f'<input type="hidden" name="{name}" value="{carried}">' in reviewed
    assert conflict.status_code == 422
    assert retry.status_code == 422
    assert counts == (0, 0, 0)
    for refused in (conflict, retry):
        body = card(refused.text, "Map project" if head == "title" else "Map outline")
        assert f'name="editing-{key}"' in body
        assert f'name="unresolved-{name}"' in body
        assert unresolved_control(body, name)
        assert unescaped(body).count(said_for(head, values[::-1] if reverse else values)) == 1


def said_for(head: str, sent: tuple[str, ...]) -> str:
    """The start of the line a note field sent twice with these answers, in this order, says
    at that field."""
    if head == "take":
        return TICK_TWICE
    if head == "title":
        return twice_said("title", sent)
    if head == "when":
        return "The due date was sent twice with different answers: "
    return "The date beside Another date was sent twice with different answers: "


FOLDED_CARRIED = [
    pytest.param("take", "0", id="tick"),
    pytest.param("title", "Another title", id="title"),
    pytest.param("when", "none", id="due-date"),
    pytest.param("when_other", "2026-11-05", id="date-box"),
]
"""Each answer a folded note's card carries hidden, with another answer to inject beside it."""


@pytest.mark.parametrize("reverse", [False, True], ids=["carried-first", "carried-last"])
@pytest.mark.parametrize(("head", "other"), FOLDED_CARRIED)
def test_a_folded_cards_carried_answer_sent_twice_shows_that_card_until_answered(
    tmp_path: pathlib.Path, head: str, other: str, reverse: bool
) -> None:
    """A note's card folded into the card for the same assignment carries its tick, title
    and due date hidden. One of them injected twice with different answers, in either order:
    nothing is written, and the review shows that note's card again with the field offered
    and marked, the answer that folded it kept as the family gave it; the returned form sent
    back unchanged writes nothing."""
    with client_in(tmp_path) as client:
        asking = homework_selected(client, TWICE_NOTED)
        folded = client.post(
            "/parent/inbox/find",
            data=ticked_form(asking) | {"occurrence-1": "update", "recheck": "1"},
        ).text
        form = ticked_form(folded)
        name = f"{head}-1"
        carried = form.pop(name)
        values = (carried, other)
        conflict = sent_as_pairs(
            client,
            "keep",
            [*form.items(), *((name, value) for value in (values[::-1] if reverse else values))],
        )
        retry = client.post("/parent/inbox/keep", data=ticked_form(conflict.text))
        counts = record_counts(client)

    assert f'<input type="hidden" name="{name}" value="{carried}">' in folded
    assert conflict.status_code == 422
    assert retry.status_code == 422
    assert f"{SUMMARY}{UNANSWERED_TWICE}" in retry.text
    assert counts == (0, 0, 0)
    anchor = "when" if head == "when_other" else head
    for refused in (conflict, retry):
        assert re.search(r'name="occurrence-1" value="update"( checked)?>', refused.text)
        assert f'<input type="hidden" name="{name}"' not in refused.text
        assert f'name="unresolved-{name}"' in refused.text
        assert unresolved_control(refused.text, f"{anchor}-1")
        said = said_for(head, values[::-1] if reverse else values)
        assert unescaped(refused.text).count(said) == 1


LINK_TWICE = 'The choice "Link her note" was sent twice with different answers. Choose one.'
APPLY_TWICE = "The choice for this instruction was sent twice with different answers. Choose one."
NONE_TWICE = (
    'The choice "No school instruction applies now" was sent twice with different answers. '
    "Choose one."
)
"""What a box to link her note, a box of an instruction that applies, and the box that no
instruction applies say at the box when sent twice with different answers."""

INSTRUCTION_FORM_UNREADABLE = (
    "The answers about the school&#39;s instructions could not be read, so nothing was saved. "
    "Look at them again below."
)
"""What the summary says when only the boxes of the school's instructions were sent twice."""

BOXES = [
    pytest.param("link-0-0", 0, LINK_TWICE, ("1", "0"), id="link"),
    pytest.param("link-0-0", 0, LINK_TWICE, ("0", "1"), id="link-reversed"),
    pytest.param("apply-1-0", 1, APPLY_TWICE, ("1", "0"), id="apply"),
    pytest.param("apply-1-0", 1, APPLY_TWICE, ("0", "1"), id="apply-reversed"),
    pytest.param("none-1", 1, NONE_TWICE, ("1", "0"), id="none"),
    pytest.param("none-1", 1, NONE_TWICE, ("0", "1"), id="none-reversed"),
]
"""A box to link her note, a box of an instruction, and the box that none applies, each an
injected duplicate submission with two different answers, in both orders, with its card."""

HER_WAITING_NOTE = "Course guide back by Friday, heard in class."


def returned_form(page: str) -> dict[str, str]:
    """A returned review as a browser sends it back untouched: ``ticked_form`` with every
    other checked box and checked choice of the page too."""
    fields = ticked_form(page)
    for name, value, rest in re.findall(
        r'<input type="(?:checkbox|radio)" name="([^"]+)" value="([^"]*)"([^>]*)>', page
    ):
        if "checked" in rest:
            fields[name] = html.unescape(value)
    return fields


def box_page(client: TestClient, field: str) -> tuple[str, str | None]:
    """The review that offers this box, with her note's id where there is one: her waiting
    note offered to link on a card of its class and title, or a joined note ticked so its
    card asks which instructions apply."""
    if field.startswith("link"):
        note = waiting_note(
            store_of(client),
            course="Health",
            title="Course Guide Due",
            text=HER_WAITING_NOTE,
            due_date=datetime.date(2026, 9, 11),
        )
        return client.post("/parent/inbox/read", data={"text": GUIDE_CARD}).text, note
    page = client.post("/parent/inbox/read", data={"text": INKED}).text
    asked = client.post("/parent/inbox/keep", data=ticked_form(page) | {"join-1-0": "1"})
    assert asked.status_code == 200
    return asked.text, None


def box_answer(field: str, value: str) -> dict[str, str]:
    """An explicit answer to the box, with what makes its card's question whole: an
    instruction's box beside the other instruction, and none applying left out beside both
    instructions."""
    if field.startswith("apply"):
        return {field: value, "apply-1-1": "1"}
    if field.startswith("none") and value == "0":
        return {field: value, "apply-1-0": "1", "apply-1-1": "1"}
    return {field: value}


def box_counts(client: TestClient, note: str | None) -> tuple[int, int, int, int]:
    """``record_counts`` with whether her note is linked to a row."""
    found = None if note is None else store_of(client).capture(note)
    return (*record_counts(client), int(found is not None and found.assignment_id is not None))


def box_today(field: str) -> str:
    """The day the review of this box is read on."""
    return "2026-09-03" if field.startswith("link") else "2026-11-03"


@pytest.mark.parametrize(("field", "key", "said", "values"), BOXES)
def test_a_box_sent_twice_with_different_answers_is_named_at_the_box(
    tmp_path: pathlib.Path, field: str, key: int, said: str, values: tuple[str, str]
) -> None:
    """A box to link her note, an instruction's box, or the box that none applies, sent twice
    with different answers, whichever came first: nothing is written, the summary says why
    with a link to its card (for the instructions' boxes, in the words the instructions' own
    refusal says), and the box says so where it is, offered as its actual choice, two
    required choices with neither chosen, marked until it is answered."""
    with client_in(tmp_path, today=box_today(field)) as client:
        page, note = box_page(client, field)
        start = box_counts(client, note)
        fields = [*returned_form(page).items(), *((field, value) for value in values)]
        refused = sent_as_pairs(client, "keep", fields)
        after = box_counts(client, note)

    notice = SENT_TWICE if field.startswith("link") else INSTRUCTION_FORM_UNREADABLE
    assert refused.status_code == 422
    assert after == start
    assert f"{SUMMARY}{notice} {CHECK_CARD.format(key=key)}" in refused.text
    assert unescaped(refused.text).count(said) == 1
    for value in ("1", "0"):
        assert f'<input type="radio" name="{field}" value="{value}" required>' in refused.text
    assert f'name="{field}" value="1" checked' not in refused.text
    assert f'name="unresolved-{field}"' in refused.text


@pytest.mark.parametrize(("field", "key", "said", "values"), BOXES)
def test_a_box_sent_twice_comes_back_and_its_unchanged_retry_writes_nothing(
    tmp_path: pathlib.Path, field: str, key: int, said: str, values: tuple[str, str]
) -> None:
    """The returned review of a box sent twice, sent back unchanged, writes nothing and says
    the choice needs an answer; so does it sent back unchanged again."""
    with client_in(tmp_path, today=box_today(field)) as client:
        page, note = box_page(client, field)
        start = box_counts(client, note)
        fields = [*returned_form(page).items(), *((field, value) for value in values)]
        refused = sent_as_pairs(client, "keep", fields)
        retry = client.post("/parent/inbox/keep", data=returned_form(refused.text))
        again = client.post("/parent/inbox/keep", data=returned_form(retry.text))
        after = box_counts(client, note)

    assert (retry.status_code, again.status_code) == (422, 422)
    assert after == start
    for answer in (retry, again):
        assert f"{SUMMARY}{UNANSWERED_TWICE}" in answer.text
        assert f'name="unresolved-{field}"' in answer.text


@pytest.mark.parametrize("answer", ["1", "0"], ids=["ticked", "unticked"])
@pytest.mark.parametrize(("field", "key", "said", "values"), BOXES)
def test_a_box_sent_twice_is_resolved_by_an_explicit_answer(
    tmp_path: pathlib.Path,
    field: str,
    key: int,
    said: str,
    values: tuple[str, str],
    answer: str,
) -> None:
    """The returned review of a box sent twice, answered explicitly either way, saves once:
    the note linked only when the answer links it, the instructions as answered."""
    with client_in(tmp_path, today=box_today(field)) as client:
        page, note = box_page(client, field)
        fields = [*returned_form(page).items(), *((field, value) for value in values)]
        refused = sent_as_pairs(client, "keep", fields)
        saved = client.post(
            "/parent/inbox/keep", data=returned_form(refused.text) | box_answer(field, answer)
        )
        counts = box_counts(client, note)

    assert saved.status_code == 303
    assert counts[0] == (1 if field.startswith("link") else 2)
    if field.startswith("link"):
        assert counts[3] == (1 if answer == "1" else 0)
