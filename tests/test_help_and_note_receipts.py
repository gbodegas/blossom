# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Gerardo Bodegas Martinez
"""A receipt of her own press is said to her alone, on Help and on a note's page.

The line Help says for the request her Ask for help form's address names, sent, already
sent, not on this page, or can't be checked, is hers: a parent signed in over her page who
opens that address reads Help as on any visit, and the page opens at its top. On a note's
page a parent is shown the result of a change either may make through the family's tree,
and never that of a change only she makes; once a newer change follows, a parent reads that
the note was saved earlier, with no word of who saved it. With the sign-in off every line is
said as to her. The fixture week through the app, a pinned day, synthetic words, no model.
"""

import pathlib
import re
import sqlite3
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Final
from urllib.parse import urlsplit

import pytest
from fastapi.testclient import TestClient

from blossom.routes.captures import (
    ADDED_TO_HOMEWORK,
    ALREADY_ADDED,
    DETAILS_SAVED,
    JOINED_TO_HOMEWORK,
    LINK_CHANGED,
    NOTE_ALREADY_SAVED,
    NOTE_ARCHIVED,
    NOTE_ASKED,
    NOTE_EDITED,
    NOTE_EDITED_IN_HOMEWORK,
    NOTE_RESTORED,
    NOTE_SAVED,
    NOTE_SAVED_EARLIER,
    UNLINKED,
)
from blossom.routes.navigation import (
    NEW_NOTE_PAGE,
    NOTE_ACTIONS,
    note_action,
    note_add_action,
    note_add_href,
    note_details_action,
    note_help_href,
    note_href,
    note_link_action,
    note_search_href,
    note_unlink_action,
)
from blossom.routes.note_details import OTHER_CLASS, choice_value
from blossom.routes.student import ALREADY_SENT, CANNOT_CHECK, NOT_ON_THIS_PAGE, SENT
from tests.support import (
    ESSAY_ID,
    HER_PAGE,
    PAGE_HEADERS,
    PLAN_DATE,
    Answer,
    help_row,
    household_client,
    lands_on,
    main_of,
    sign_in_as,
    state_of,
    went_to,
    whole_form,
    words,
)

READERS: Final = ("her", "parent", "open")
HELP_RESULT: Final = '<p class="note update-result" role="status" id="help-result" tabindex="-1">'
NOTE_RESULT: Final = '<p class="note update-result" role="status" id="note-result" tabindex="-1">'
HELP_LINES: Final = (SENT, ALREADY_SENT, NOT_ON_THIS_PAGE, CANNOT_CHECK)
WAITING: Final = "Waiting for a parent to respond."
TAKEN_UP: Final = "A parent is on it."
CLOSED: Final = "A parent closed this request on "
NOBODY: Final = "0" * 32
"""An id of the right shape that names no request."""
HER_UNREADABLE: Final = "requests for help can't be read right now. Refresh replies to check them"
PARENTS_UNREADABLE: Final = (
    "Her requests for help can't be read right now. Refresh replies to try again."
)
FOLD_OPEN: Final = '<details class="steps resolved" id="help-older" open>'
FOLD_CLOSED: Final = FOLD_OPEN.replace(" open>", ">")
PARENTS_EARLIER: Final = (
    "This note was saved earlier and has changed since. This page shows it as it stands now."
)
WORDS: Final = "Geometry questions 4-8, heard in class"
REFUSED: Final = "the file cannot be read"


def over_her_tab(client: TestClient, reader: str) -> None:
    """Who opens the address she left: she stays, the household stays, or a parent signs in
    on her device."""
    if reader == "parent":
        client.post("/sign-out")
        sign_in_as(client, "parent")


def no_autofocus(page: str) -> bool:
    return re.search(r"\sautofocus(?=[\s>])", page) is None


def help_part(page: str) -> str:
    """Help, from its heading up to the row list: where a line with no row to go beside is
    said, and what a parent is told about her requests."""
    start = page.index('<section class="panel help-panel" aria-labelledby="help">')
    end = page.find('<div id="help-updates"', start)
    return page[start : end if end >= 0 else page.index("</section>", start)]


# ------------------------------------------------------------------ Help's line for an address


CASES: Final = (
    "waiting",
    "taken up",
    "closed a week ago",
    "a well-formed id not on the page",
    "a malformed id",
    "the file refused",
    "a row unreadable",
)
FAILED: Final = ("the file refused", "a row unreadable")


def her_line(marker: str, case: str) -> str:
    """What Help says to her about the address."""
    if case in FAILED:
        return CANNOT_CHECK
    if case in ("a well-formed id not on the page", "a malformed id"):
        return NOT_ON_THIS_PAGE
    return SENT if marker == "asked" and case == "waiting" else ALREADY_SENT


def asked_before(client: TestClient, case: str) -> tuple[str, str]:
    """Two requests of hers, the second moved as ``case`` says; both ids."""
    store = state_of(client).help_requests
    other = store.ask(PLAN_DATE, "Synthetic question about the essay").request_id
    named = store.ask(PLAN_DATE, "Synthetic question about the graph").request_id
    if case == "taken up":
        store.accept(named, "Synthetic reply")
    elif case == "closed a week ago":
        store.resolve(named, "Synthetic reply")
        # The store ages a request by the real clock: closed eight days ago puts it in the fold.
        eight_days_ago = (datetime.now(UTC) - timedelta(days=8)).isoformat()
        store._connection.execute(
            "UPDATE help_requests SET resolved_at = ? WHERE request_id = ?",
            (eight_days_ago, named),
        )
        store._connection.commit()
    return other, named


def unreadable(client: TestClient, case: str, monkeypatch: pytest.MonkeyPatch) -> None:
    """Her requests made unreadable as ``case`` says: the file refuses the read, or a row
    can't be read as a request."""
    store = state_of(client).help_requests
    if case == "the file refused":

        def refuses() -> None:
            raise sqlite3.OperationalError(REFUSED)

        monkeypatch.setattr(store, "retained", refuses)
    elif case == "a row unreadable":
        store._connection.execute("UPDATE help_requests SET evening = 'not a date'")
        store._connection.commit()


@pytest.mark.parametrize("reader", READERS)
@pytest.mark.parametrize("case", CASES)
@pytest.mark.parametrize("marker", ["asked", "asked_again"])
def test_helps_line_for_an_address_is_said_to_her_and_never_to_a_parent(
    marker: str,
    case: str,
    reader: str,
    tmp_path: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Each marker, naming a request on the page, one that is not, or on a failed read, for
    her, a parent signed in over her page, and the sign-in off. A parent's page is the page
    with no marker at all."""
    with household_client(reader, tmp_path) as client:
        sign_in_as(client, "her" if reader == "parent" else reader)
        other, named = asked_before(client, case)
        over_her_tab(client, reader)
        unreadable(client, case, monkeypatch)
        value = {"a well-formed id not on the page": NOBODY, "a malformed id": "not-an-id"}
        address = f"{HER_PAGE}?{marker}={value.get(case, named)}#help-result"
        answer = client.get(address, headers=PAGE_HEADERS)
        plain = client.get(HER_PAGE, headers=PAGE_HEADERS)

    assert answer.status_code == 200
    assert no_autofocus(answer.text)
    page = main_of(answer.text)
    if case in FAILED:
        assert 'class="help-request"' not in page
    else:
        assert WAITING in words(help_row(page, other))
        stands = {"taken up": TAKEN_UP, "closed a week ago": CLOSED}.get(case, WAITING)
        assert stands in words(help_row(page, named))
        assert (FOLD_OPEN in page) is (case == "closed a week ago" and reader != "parent")
    if reader == "parent":
        assert page == main_of(plain.text)
        assert lands_on(page, address) == ""
        assert 'id="help-result"' not in page
        assert not [line for line in HELP_LINES if line in words(page)]
        assert (PARENTS_UNREADABLE in words(help_part(page))) is (case in FAILED)
        assert HER_UNREADABLE not in words(page)
        return
    line = her_line(marker, case)
    assert page.count('id="help-result"') == 1
    assert lands_on(page, address) == HELP_RESULT
    assert [said for said in HELP_LINES if said in words(page)] == [line]
    where = help_part(page) if line in (NOT_ON_THIS_PAGE, CANNOT_CHECK) else help_row(page, named)
    assert f"{HELP_RESULT}{line}" in where.replace("&#39;", "'")
    assert (HER_UNREADABLE in words(help_part(page))) is (case in FAILED)
    assert PARENTS_UNREADABLE not in words(page)


def test_a_parent_signing_in_over_her_page_after_she_asked_reads_no_line(
    tmp_path: pathlib.Path,
) -> None:
    """Her real presses: a fresh ask, the same form sent again, and a note's help form sent
    again, each redirected to an address that says so; then a parent signs in on her device
    and opens each address."""
    ask = "/student/actions/ask-for-help"
    with household_client("her", tmp_path) as client:
        sign_in_as(client, "her")
        form = {**whole_form(client.get(HER_PAGE).text, ask), "note": "Synthetic question"}
        fresh = went_to(client.post(ask, data=form))
        again = went_to(client.post(ask, data=form))
        name = a_note(client)
        about = note_action(name, "ask-for-help")
        note_form = {**whole_form(client.get(note_help_href(name)).text, about), "note": "Why?"}
        went_to(client.post(about, data=note_form))
        note_again = went_to(client.post(about, data=note_form))
        hers = [client.get(where).text for where in (fresh, again, note_again)]
        over_her_tab(client, "parent")
        theirs = [client.get(where).text for where in (fresh, again, note_again)]
        plain = client.get(HER_PAGE).text

    assert "?asked=" in fresh
    assert "?asked_again=" in again
    assert "?asked_again=" in note_again
    assert [said for page in hers for said in HELP_LINES if said in page] == [
        SENT,
        ALREADY_SENT,
        ALREADY_SENT,
    ]
    for where, page in zip((fresh, again, note_again), theirs, strict=True):
        shown = main_of(page)
        assert shown == main_of(plain)
        assert lands_on(shown, where) == ""
        assert not [line for line in HELP_LINES if line in words(shown)]
        assert no_autofocus(page)
    assert words(main_of(plain)).count("She asked for help") == 2
    assert words(main_of(plain)).count(WAITING) == 2


# ------------------------------------------------------------------ a note's page


def first_save(client: TestClient, times: int = 1) -> tuple[str, dict[str, str]]:
    """A new note of hers, its form sent ``times`` times; the last address and the form."""
    page = client.get(NEW_NOTE_PAGE, headers=PAGE_HEADERS).text
    form = {**whole_form(page, NOTE_ACTIONS), "text": WORDS, "course": "", "due_date": ""}
    sent = [client.post(NOTE_ACTIONS, data=form, headers=PAGE_HEADERS) for _ in range(times)]
    return went_to(sent[-1]), form


def a_note(client: TestClient) -> str:
    return first_save(client)[1]["capture_id"]


def name_of(address: str) -> str:
    """The note an address of a note's page names."""
    return urlsplit(address).path.rsplit("/", 1)[1]


def pressed(client: TestClient, name: str, step: str, times: int = 1, **typed: str) -> str:
    """Her press of one of her note's own forms, ``edit``, ``archive``, ``restore`` or
    ``ask-for-help``, from the page that shows it, sent ``times`` times; the last address."""
    shown = {"edit": note_href(name, edit="1"), "ask-for-help": note_help_href(name)}
    action = note_action(name, step)
    page = client.get(shown.get(step, note_href(name)), headers=PAGE_HEADERS).text
    form = {**whole_form(page, action), **typed}
    sent = [client.post(action, data=form, headers=PAGE_HEADERS) for _ in range(times)]
    return went_to(sent[-1])


def details_sent(
    client: TestClient,
    name: str,
    *,
    add: bool = False,
    family: bool = False,
    times: int = 1,
    **typed: str,
) -> Answer:
    """The details form of a note, filled in and sent through Save details or Add to
    homework ``times`` times; the last answer."""
    form = whole_form(
        client.get(note_add_href(name, family=family), headers=PAGE_HEADERS).text,
        note_add_action(name, family=family),
    )
    given = {"course_choice": OTHER_CLASS, "course_other": "Geometry", "title": "Questions 4-8"}
    form = {**form, **given, "due_date": "", "kind": "HOMEWORK", "note": "", **typed}
    action = (note_add_action if add else note_details_action)(name, family=family)
    sent = [client.post(action, data=form, headers=PAGE_HEADERS) for _ in range(times)]
    return sent[-1]


def found_and_linked(client: TestClient, name: str, search: str, target: str) -> str:
    """Her search for homework already here, and her press on the row for ``target``."""
    page = client.get(note_search_href(name, q=search), headers=PAGE_HEADERS).text
    row = page.split(f'id="found-{target}"')[1].split("</li>")[0]
    action = note_link_action(name)
    return went_to(client.post(action, data=whole_form(row, action), headers=PAGE_HEADERS))


@dataclass(frozen=True)
class Said:
    """The address a press left and the sentence its result starts with."""

    address: str
    said: str


def first_saved(client: TestClient) -> Said:
    return Said(first_save(client)[0], NOTE_SAVED)


def first_saved_again(client: TestClient) -> Said:
    return Said(first_save(client, times=2)[0], NOTE_ALREADY_SAVED)


def saved_and_changed_since(client: TestClient) -> Said:
    where, form = first_save(client)
    pressed(client, form["capture_id"], "edit", text="Changed words")
    return Said(where, NOTE_SAVED_EARLIER)


def edited(client: TestClient) -> Said:
    return Said(pressed(client, a_note(client), "edit", text="Changed words"), NOTE_EDITED)


def edited_to_the_same(client: TestClient) -> Said:
    return Said(pressed(client, a_note(client), "edit"), NOTE_ALREADY_SAVED)


def edited_in_homework(client: TestClient) -> Said:
    name = a_note(client)
    went_to(details_sent(client, name, add=True))
    return Said(pressed(client, name, "edit", text="Changed words"), NOTE_EDITED_IN_HOMEWORK)


def archived(client: TestClient) -> Said:
    return Said(pressed(client, a_note(client), "archive"), NOTE_ARCHIVED)


def archived_again(client: TestClient) -> Said:
    return Said(pressed(client, a_note(client), "archive", times=2), NOTE_ALREADY_SAVED)


def restored(client: TestClient) -> Said:
    name = a_note(client)
    pressed(client, name, "archive")
    return Said(pressed(client, name, "restore"), NOTE_RESTORED)


def restored_again(client: TestClient) -> Said:
    name = a_note(client)
    pressed(client, name, "archive")
    return Said(pressed(client, name, "restore", times=2), NOTE_ALREADY_SAVED)


def asked_about(client: TestClient) -> Said:
    return Said(pressed(client, a_note(client), "ask-for-help", note="Which part?"), NOTE_ASKED)


def details_saved(client: TestClient) -> Said:
    return Said(went_to(details_sent(client, a_note(client))), DETAILS_SAVED)


def details_saved_again(client: TestClient) -> Said:
    return Said(went_to(details_sent(client, a_note(client), times=2)), NOTE_ALREADY_SAVED)


def added(client: TestClient) -> Said:
    return Said(went_to(details_sent(client, a_note(client), add=True)), ADDED_TO_HOMEWORK)


def added_again(client: TestClient) -> Said:
    return Said(went_to(details_sent(client, a_note(client), add=True, times=2)), ALREADY_ADDED)


def added_as_the_same_homework(client: TestClient) -> Said:
    """Added with the class and title of the essay on record, and the essay chosen."""
    name = a_note(client)
    asked = details_sent(
        client,
        name,
        add=True,
        course_choice="World History",
        course_other="",
        title="Canal Era comparison essay",
    )
    assert asked.status_code == 409, asked.text[:400]
    action = note_add_action(name)
    form = {**whole_form(asked.text, action), "candidate": choice_value(ESSAY_ID)}
    joined = client.post(action, data=form, headers=PAGE_HEADERS)
    return Said(went_to(joined), JOINED_TO_HOMEWORK)


def joined_by_search(client: TestClient) -> Said:
    return Said(found_and_linked(client, a_note(client), "canal", ESSAY_ID), JOINED_TO_HOMEWORK)


def moved_by_search(client: TestClient) -> Said:
    name = a_note(client)
    found_and_linked(client, name, "canal", ESSAY_ID)
    return Said(found_and_linked(client, name, "reading", "assignment-reading-log"), LINK_CHANGED)


def unlinked(client: TestClient) -> Said:
    name = a_note(client)
    found_and_linked(client, name, "canal", ESSAY_ID)
    action = note_unlink_action(name)
    form = whole_form(client.get(note_add_href(name), headers=PAGE_HEADERS).text, action)
    return Said(went_to(client.post(action, data=form, headers=PAGE_HEADERS)), UNLINKED)


HERS_ALONE: Final[dict[str, Callable[[TestClient], Said]]] = {
    "a first save": first_saved,
    "the same first save again": first_saved_again,
    "a first save, changed since": saved_and_changed_since,
    "an edit": edited,
    "an edit that changed nothing": edited_to_the_same,
    "an edit of a note in homework": edited_in_homework,
    "an archive": archived,
    "an archive sent again": archived_again,
    "a restore": restored,
    "a restore sent again": restored_again,
    "a request for help about it": asked_about,
}
EITHERS: Final[dict[str, Callable[[TestClient], Said]]] = {
    "the details saved": details_saved,
    "the same details again": details_saved_again,
    "added to homework": added,
    "added again": added_again,
    "added as homework already here": added_as_the_same_homework,
    "joined by search": joined_by_search,
    "the link moved": moved_by_search,
    "unlinked": unlinked,
}


def opened_by(
    reader: str, tmp_path: pathlib.Path, make: Callable[[TestClient], Said]
) -> tuple[Said, Answer]:
    """Her press, then the address it left, opened by her, by a parent who signed in over
    her page, or with the sign-in off, where the household pressed."""
    with household_client(reader, tmp_path) as client:
        sign_in_as(client, "her" if reader == "parent" else reader)
        left = make(client)
        over_her_tab(client, reader)
        return left, client.get(left.address, headers=PAGE_HEADERS)


def said_first(page: str, address: str, said: str) -> bool:
    """Whether the address lands on the result, and the result starts with ``said``."""
    if lands_on(page, address) != NOTE_RESULT:
        return False
    start = page.index(NOTE_RESULT)
    return words(page[start : page.index("</p>", start)]).startswith(said)


@pytest.mark.parametrize("reader", READERS)
@pytest.mark.parametrize("surface", list(HERS_ALONE))
def test_the_result_of_a_change_only_she_makes_is_hers(
    surface: str, reader: str, tmp_path: pathlib.Path
) -> None:
    left, answer = opened_by(reader, tmp_path, HERS_ALONE[surface])

    assert answer.status_code == 200
    assert no_autofocus(answer.text)
    page = main_of(answer.text)
    if reader == "parent":
        assert lands_on(page, left.address) == ""
        assert "update-result" not in page
        assert left.said not in words(page)
        assert "What she wrote" in page
    else:
        assert said_first(page, left.address, left.said)


@pytest.mark.parametrize("reader", READERS)
@pytest.mark.parametrize("surface", list(EITHERS))
def test_the_result_of_a_change_either_may_make_is_said_to_every_reader(
    surface: str, reader: str, tmp_path: pathlib.Path
) -> None:
    left, answer = opened_by(reader, tmp_path, EITHERS[surface])

    assert answer.status_code == 200
    assert no_autofocus(answer.text)
    page = main_of(answer.text)
    assert said_first(page, left.address, left.said)
    assert ("What she wrote" in page) is (reader == "parent")


@pytest.mark.parametrize("button", ["details", "add"])
def test_a_parent_reads_the_result_of_a_change_made_through_the_familys_tree(
    button: str, tmp_path: pathlib.Path
) -> None:
    with household_client("parent", tmp_path) as client:
        sign_in_as(client, "her")
        name = a_note(client)
        over_her_tab(client, "parent")
        where = went_to(details_sent(client, name, add=button == "add", family=True))
        answer = client.get(where, headers=PAGE_HEADERS)

    page = main_of(answer.text)
    assert said_first(page, where, DETAILS_SAVED if button == "details" else ADDED_TO_HOMEWORK)
    assert no_autofocus(answer.text)


@pytest.mark.parametrize("reader", READERS)
@pytest.mark.parametrize("surface", list(EITHERS))
def test_a_shared_result_a_newer_change_follows_names_nobody_to_a_parent(
    surface: str, reader: str, tmp_path: pathlib.Path
) -> None:
    """Each shared result, then her edit: the result is something saved earlier, said to a
    parent without naming who saved it, and to her and the sign-in off in their own words."""

    def then_edited(client: TestClient) -> Said:
        left = EITHERS[surface](client)
        pressed(client, name_of(left.address), "edit", text="Changed words")
        return left

    left, answer = opened_by(reader, tmp_path, then_edited)

    assert answer.status_code == 200
    assert no_autofocus(answer.text)
    page = main_of(answer.text)
    said, never = (NOTE_SAVED_EARLIER, PARENTS_EARLIER)
    if reader == "parent":
        said, never = never, said
    assert said_first(page, left.address, said)
    assert never not in words(page)


def test_an_address_naming_her_edit_with_the_details_word_reads_the_neutral_line(
    tmp_path: pathlib.Path,
) -> None:
    """Compatibility with addresses already saved: ``unchanged`` naming the change her edit
    that wrote nothing found reads to a parent as the details' result does, in the neutral
    line, which names nobody. Her own saves that write nothing say ``same``."""
    with household_client("parent", tmp_path) as client:
        sign_in_as(client, "her")
        mine = edited_to_the_same(client).address
        kept = mine.replace("said=same", "said=unchanged")
        over_her_tab(client, "parent")
        answers = [client.get(where, headers=PAGE_HEADERS) for where in (mine, kept)]

    assert "said=same" in mine
    assert 'id="note-result"' not in main_of(answers[0].text)
    assert said_first(main_of(answers[1].text), kept, NOTE_ALREADY_SAVED)


@pytest.mark.parametrize("reader", READERS)
@pytest.mark.parametrize("marker", ["asked", "asked_again"])
def test_a_request_in_the_older_fold_opens_it_for_her_and_leaves_it_closed_for_a_parent(
    marker: str, reader: str, tmp_path: pathlib.Path
) -> None:
    """A parent's Help is the view with no marker: the fold stays closed with the request in
    it, and nothing takes the focus. Her page opens it at her line."""
    with household_client(reader, tmp_path) as client:
        sign_in_as(client, "her" if reader == "parent" else reader)
        _, named = asked_before(client, "closed a week ago")
        over_her_tab(client, reader)
        address = f"{HER_PAGE}?{marker}={named}#help-result"
        answer = client.get(address, headers=PAGE_HEADERS)
        plain = client.get(HER_PAGE, headers=PAGE_HEADERS)

    page = main_of(answer.text)
    fold = page[page.index(FOLD_CLOSED.removesuffix(">")) :]
    fold = fold[: fold.index("</details>")]
    assert CLOSED in words(help_row(fold, named))
    assert no_autofocus(answer.text)
    if reader == "parent":
        assert fold.startswith(FOLD_CLOSED)
        assert lands_on(page, address) == ""
        assert page == main_of(plain.text)
    else:
        assert fold.startswith(FOLD_OPEN)
        assert lands_on(page, address) == HELP_RESULT
        assert f"{HELP_RESULT}{ALREADY_SENT}" in help_row(fold, named)
