"""Finding homework already here and joining a note to it, or correcting the link: the pages.

The note's page offers the search; the search page finds homework by its
class or title, twenty to a page, and each result carries the press that joins
the note to it, held to the row as shown and to the note's revision. A joined
note can be moved or unlinked from its page and from the details page, in
both trees; a note that made its own assignment offers neither. Refusals keep
the search words and the choice; the other person's press is refused before
the note's name is read; nothing is joined by a search alone.
"""

import pathlib
import sqlite3
from collections.abc import Callable
from datetime import date
from typing import cast

import pytest
from fastapi.testclient import TestClient
from markupsafe import escape

from blossom import intake
from blossom.app import create_app
from blossom.candidates import readings_for, row_reader
from blossom.captures import (
    STUDENT,
    CandidatesChanged,
    CaptureNotSaved,
    HomeworkGone,
    candidate_basis,
    derived_assignment_id,
)
from blossom.homework_search import PAGE_SIZE
from blossom.reconciliation import SourceChannel
from blossom.routes.captures import (
    ALREADY_ADDED,
    JOINED_TO_HOMEWORK,
    LINK_CHANGED,
    NOTE_CHANGED,
    NOTE_GONE,
    NOTE_UNREADABLE,
    UNLINKED,
)
from blossom.routes.navigation import (
    NEW_NOTE_PAGE,
    NOTE_ACTIONS,
    NOTES_PAGE,
    note_action,
    note_add_action,
    note_add_href,
    note_href,
    note_link_action,
    note_search_href,
    note_unlink_action,
)
from blossom.routes.note_details import NOT_A_PARENTS_PRESS, NOT_SAVED, OTHER_CLASS
from blossom.routes.note_links import (
    HOMEWORK_CHANGED,
    HOMEWORK_GONE,
    NO_CHOICE,
    NO_SUCH_PAGE,
    NOT_JOINED,
    NOTHING_FOUND,
    QUERY_REFUSED,
    SEARCH_WORDS_NEEDED,
)
from blossom.routes.student import BAD_FORM, NOT_HERS_TO_UPDATE
from blossom.stores.project_state import Assignment, ProjectStateStore
from tests.support import (
    HERS,
    PAGE_HEADERS,
    SAME_ORIGIN,
    THEIRS,
    browser,
    form_fields,
    report,
    signed_in_household,
    state_of,
    whole_form,
)

WORDS = "The reading log, the one Ms. Ortiz mentioned"


def save_note(client: TestClient, text: str = WORDS, due_date: str = "") -> str:
    fields = form_fields(client.get(NEW_NOTE_PAGE).text, NOTE_ACTIONS)
    answer = client.post(
        NOTE_ACTIONS,
        data={**fields, "text": text, "course": "", "due_date": due_date},
        headers=PAGE_HEADERS,
    )
    assert answer.status_code == 303, answer.text[:300]
    return answer.headers["location"].split("/homework-notes/")[1].split("?")[0].split("#")[0]


def on_record(
    client: TestClient,
    course: str = "Humanities",
    title: str = "Summer reading log",
    due: date | None = date(2026, 8, 28),
) -> Assignment:
    row = Assignment(
        assignment_id=intake.identity(course, title, None if due is None else due.isoformat()),
        course=course,
        title=title,
        due_date=due,
        dependencies=[],
        reported_submission_status="not_started",
        origins={"record": SourceChannel.LMS},
    )
    state_of(client).project_state.put_on_record([row], {})
    return row


def rows(client: TestClient) -> dict[str, list[object]]:
    connection = state_of(client).project_state._connection
    return {
        name: connection.execute(f"SELECT * FROM {name} ORDER BY rowid").fetchall()  # noqa: S608
        for name in ("assignments", "date_claims", "homework_captures", "capture_events")
    }


def claims(client: TestClient, assignment_id: str) -> list[tuple[object, ...]]:
    return [
        tuple(row)
        for row in state_of(client)
        .project_state._connection.execute(
            "SELECT asserted_value, capture_id IS NOT NULL, active FROM date_claims "
            "WHERE assignment_id = ? ORDER BY rowid",
            (assignment_id,),
        )
        .fetchall()
    ]


def search(client: TestClient, name: str, q: str, *, family: bool = False, **more: str) -> str:
    answer = client.get(note_search_href(name, family=family, q=q, **more), headers=PAGE_HEADERS)
    assert answer.status_code == 200, answer.text[:300]
    return answer.text


def press_of(page: str, name: str, target: str, *, family: bool = False) -> dict[str, str]:
    """The link press for one result, as the page holds it."""
    row = page.split(f'id="found-{target}"')[1].split("</li>")[0]
    return whole_form(row, note_link_action(name, family=family))


def unlink_press(page: str, name: str, *, family: bool = False) -> dict[str, str]:
    return whole_form(page, note_unlink_action(name, family=family))


def joined(client: TestClient, name: str, target: Assignment) -> None:
    page = search(client, name, target.title.split()[0])
    answer = client.post(note_link_action(name), data=press_of(page, name, target.assignment_id))
    assert answer.status_code == 303, answer.text[:300]


# ------------------------------------------------------------------ where the search is offered


def test_the_note_and_the_details_page_offer_the_search_and_a_search_joins_nothing() -> None:
    with browser() as client:
        name = save_note(client)
        on_record(client)
        note_page = client.get(note_href(name)).text
        details = client.get(note_add_href(name)).text
        before = rows(client)
        page = search(client, name, "reading")
        after = rows(client)

    assert f'<a href="{note_search_href(name)}">Link to homework already here</a>' in note_page
    assert f'<a href="{note_search_href(name)}">Search homework already here</a>' in details
    assert "<h1>Link a note to homework already here</h1>" in page
    assert escape(WORDS) in page
    assert after == before


def test_the_search_reads_class_and_title_and_shows_each_result_with_enough_to_choose() -> None:
    """Every word must occur in the class or the title, whatever its case or spacing; Done
    work and work outside this week are found; a row shows the class, the title, the date,
    the kind, what she says, and what the school says; each row carries its own press with
    the note's revision and the row's fingerprint; her words are not searched."""
    with browser() as client:
        name = save_note(client)
        log = on_record(client)
        done = on_record(client, title="Reading log, unit two", due=date(2026, 6, 5))
        on_record(
            client, course="Spanish", title="Vocabulary list, unit nine", due=date(2026, 8, 21)
        )
        report(client, done.assignment_id, "done")
        page = search(client, name, "  READING   log ")
        by_words = search(client, name, "Ortiz")
        spanish = search(client, name, "spanish nine")

    assert page.count('candidate-facts"') == 3
    assert page.index(f'id="found-{done.assignment_id}"') < page.index(
        f'id="found-{log.assignment_id}"'
    )
    row = page.split(f'id="found-{done.assignment_id}"')[1].split("</li>")[0]
    assert "Reading log, unit two" in row
    assert "due June 5, 2026" in row
    assert "Kind: Homework." in row
    assert "You said Done on" in row
    press = press_of(page, name, log.assignment_id)
    assert press["target"] == log.assignment_id
    assert press["revision"] == "1"
    assert len(press["basis"]) == 64
    assert "from" not in press
    assert ">Link to this homework</button>" in page
    assert 'id="found-' not in by_words
    assert NOTHING_FOUND in by_words
    assert spanish.count('id="found-') == 1


def test_an_empty_query_asks_for_words_and_a_query_the_rules_refuse_is_refused() -> None:
    with browser() as client:
        name = save_note(client)
        on_record(client)
        empty = search(client, name, "")
        blank = search(client, name, "   ")
        long = client.get(note_search_href(name, q="q" * 201), headers=PAGE_HEADERS)
        broken = client.get(
            note_search_href(name, q="two" + chr(10) + "lines"), headers=PAGE_HEADERS
        )

    assert SEARCH_WORDS_NEEDED in empty
    assert SEARCH_WORDS_NEEDED in blank
    assert 'id="found-' not in empty
    assert long.status_code == 200
    assert QUERY_REFUSED in long.text
    assert 'id="found-' not in long.text
    assert QUERY_REFUSED in broken.text


def test_results_come_twenty_to_a_page_in_one_order_with_next_and_previous() -> None:
    with browser() as client:
        name = save_note(client)
        for n in range(25):
            on_record(client, course="Math", title=f"Drill {n:02d}", due=date(2026, 9, 1))
        first = search(client, name, "drill")
        second = search(client, name, "drill", page="2")
        third = client.get(note_search_href(name, q="drill", page="3"), headers=PAGE_HEADERS)
        padded = client.get(note_search_href(name, q="drill", page="01"), headers=PAGE_HEADERS)
        letters = client.get(note_search_href(name, q="drill", page="x"), headers=PAGE_HEADERS)
        beyond_nothing = client.get(note_search_href(name, q="", page="2"), headers=PAGE_HEADERS)

    assert first.count('id="found-') == PAGE_SIZE
    assert "Found 25, page 1 of 2" in first
    assert ">Next page</a>" in first
    assert ">Previous page</a>" not in first
    assert f'href="{escape(note_search_href(name, q="drill", page="2"))}"' in first
    assert second.count('id="found-') == 5
    assert ">Previous page</a>" in second
    assert ">Next page</a>" not in second
    assert "Drill 24" in second
    assert "Drill 00" in first
    assert first.index("Drill 00") < first.index("Drill 01")
    for answer in (third, padded, letters, beyond_nothing):
        assert answer.status_code == 404
        assert NO_SUCH_PAGE in answer.text
        assert 'id="found-' not in answer.text


# ------------------------------------------------------------------ the link press


def test_pressing_a_result_joins_the_note_and_the_same_press_again_writes_nothing() -> None:
    with browser() as client:
        name = save_note(client, due_date="2026-08-21")
        log = on_record(client)
        page = search(client, name, "reading")
        press = press_of(page, name, log.assignment_id)
        answer = client.post(note_link_action(name), data=press)
        landed = client.get(answer.headers["location"]).text
        after = rows(client)
        claimed = claims(client, log.assignment_id)
        again = client.post(note_link_action(name), data=press)
        after_again = rows(client)
        note_page = client.get(note_href(name)).text
        details = client.get(note_add_href(name)).text
        assignment = client.get(f"/student/assignments/{log.assignment_id}").text
        queue = client.get(NOTES_PAGE).text

    assert answer.status_code == 303
    assert escape(JOINED_TO_HOMEWORK) in landed
    assert claimed == [("2026-08-21", 1, 1)]
    assert again.status_code == 303
    assert after_again == after
    assert "It was joined to homework that was here before it." in note_page
    assert ">Change link</a>" in note_page
    assert ">Unlink from this homework</button>" in note_page
    assert f'action="{note_unlink_action(name)}"' in note_page
    assert ">Change link</a>" in details
    assert f'action="{note_unlink_action(name)}"' in details
    assert escape(WORDS) in assignment
    assert escape(WORDS) not in queue


def test_a_link_press_is_held_to_the_row_shown_and_to_the_note() -> None:
    """Homework that changed in anything the row showed is put to the person again, with the
    words and the choice kept; a page that is behind the note is refused; homework that left
    the record is said so; nothing is written for any of them."""
    with browser() as client:
        name = save_note(client)
        log = on_record(client)
        page = search(client, name, "reading")
        press = press_of(page, name, log.assignment_id)
        report(client, log.assignment_id, "done")
        before = rows(client)
        changed = client.post(note_link_action(name), data=press)
        fresh = press_of(changed.text, name, log.assignment_id)
        behind = client.post(note_link_action(name), data={**fresh, "revision": "2"})
        state_of(client).project_state._connection.execute(
            "DELETE FROM assignments WHERE assignment_id = ?", (log.assignment_id,)
        )
        state_of(client).project_state._connection.commit()
        gone = client.post(note_link_action(name), data=fresh)
        after = rows(client)

    assert changed.status_code == 409
    assert changed.text.count(" autofocus") == 1
    assert HOMEWORK_CHANGED in changed.text
    assert 'value="reading"' in changed.text
    assert "You said Done on" in changed.text
    assert fresh["basis"] != press["basis"]
    assert behind.status_code == 409
    assert escape(NOTE_CHANGED) in behind.text
    assert gone.status_code == 409
    assert HOMEWORK_GONE in gone.text
    assert after["homework_captures"] == before["homework_captures"]
    assert after["capture_events"] == before["capture_events"]


def test_a_form_these_pages_did_not_make_is_refused_with_the_words_kept() -> None:
    with browser() as client:
        name = save_note(client)
        log = on_record(client)
        press = press_of(search(client, name, "reading"), name, log.assignment_id)
        before = rows(client)
        no_choice = client.post(note_link_action(name), data={**press, "target": ""})
        no_basis = client.post(
            note_link_action(name), data={k: v for k, v in press.items() if k != "basis"}
        )
        stray = client.post(note_link_action(name), data={**press, "extra": "field"})
        after = rows(client)

    assert no_choice.status_code == 422
    assert NO_CHOICE in no_choice.text
    assert 'value="reading"' in no_choice.text
    assert no_basis.status_code == 422
    assert escape(BAD_FORM) in no_basis.text
    assert stray.status_code == 422
    assert after == before


def test_a_store_failure_is_said_honestly_with_the_words_kept(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    with browser() as client:
        name = save_note(client)
        log = on_record(client)
        press = press_of(search(client, name, "reading"), name, log.assignment_id)
        store = state_of(client).project_state

        def refuses(*args: object, **kwargs: object) -> object:
            raise CaptureNotSaved(name, RuntimeError("the file refused"))

        monkeypatch.setattr(store, "link_capture", refuses)
        answer = client.post(note_link_action(name), data=press)

    assert answer.status_code == 500
    assert escape(NOT_SAVED) in answer.text
    assert answer.text.count(" autofocus") == 1
    assert 'value="reading"' in answer.text


# ------------------------------------------------------------------ unlinking and changing a link


def test_unlinking_puts_the_note_back_in_homework_notes_and_withdraws_only_its_claim() -> None:
    with browser() as client:
        name = save_note(client, due_date="2026-08-21")
        log = on_record(client)
        joined(client, name, log)
        note_page = client.get(note_href(name)).text
        press = unlink_press(note_page, name)
        before = rows(client)
        answer = client.post(note_unlink_action(name), data=press)
        landed = client.get(answer.headers["location"]).text
        claimed = claims(client, log.assignment_id)
        again = client.post(note_unlink_action(name), data=press)
        after = rows(client)
        queue = client.get(NOTES_PAGE).text
        assignment = client.get(f"/student/assignments/{log.assignment_id}").text
        waiting = client.get(note_href(name)).text

    assert answer.status_code == 303
    assert escape(UNLINKED) in landed
    assert claimed == [("2026-08-21", 1, 0)]
    assert after["assignments"] == before["assignments"]
    assert again.status_code == 303
    assert escape(WORDS) in queue
    assert "Withdrawn claims about the date" in assignment
    assert "From a homework note." in assignment
    assert escape(WORDS) not in assignment.split("Withdrawn claims about the date")[0]
    assert ">Link to homework already here</a>" in waiting
    assert "Unlinked from homework" in waiting
    assert ">Unlink from this homework</button>" not in waiting


def test_changing_a_link_moves_the_note_and_says_so() -> None:
    with browser() as client:
        name = save_note(client, due_date="2026-08-21")
        log = on_record(client)
        other = on_record(
            client, course="Spanish", title="Vocabulary list, unit nine", due=date(2026, 8, 21)
        )
        joined(client, name, log)
        page = search(client, name, "nine")
        press = press_of(page, name, other.assignment_id)
        answer = client.post(note_link_action(name), data=press)
        landed = client.get(answer.headers["location"]).text
        old_claims = claims(client, log.assignment_id)
        new_claims = claims(client, other.assignment_id)
        again = client.post(note_link_action(name), data=press)
        note_page = client.get(note_href(name)).text

    assert "already linked to homework" in page
    assert "Summer reading log" in page.split("Linked now to")[1].split("</p>")[0]
    assert press["from"] == log.assignment_id
    assert ">Move the link here</button>" in page
    assert answer.status_code == 303
    assert escape(LINK_CHANGED) in landed
    assert old_claims == [("2026-08-21", 1, 0)]
    assert new_claims == [("2026-08-21", 1, 1)]
    assert again.status_code == 303
    assert f'href="/student/assignments/{other.assignment_id}' in note_page


def test_a_note_that_made_its_own_assignment_offers_no_change_or_unlink() -> None:
    with browser() as client:
        name = save_note(client)
        details = client.get(note_add_href(name)).text
        form = form_fields(details, note_add_action(name))
        added = client.post(
            note_add_action(name),
            data={
                **form,
                "course_choice": OTHER_CLASS,
                "course_other": "Geometry",
                "title": "Questions 4-8",
                "kind": "HOMEWORK",
                "due_date": "",
                "note": "",
            },
        )
        assert added.status_code == 303, added.text[:400]
        own = derived_assignment_id(name)
        note_page = client.get(note_href(name)).text
        details = client.get(note_add_href(name)).text
        page = search(client, name, "reading")
        before = rows(client)
        unlinked = client.post(note_unlink_action(name), data={"revision": "2", "from": own})
        after = rows(client)

    assert ">Unlink from this homework</button>" not in note_page
    assert ">Change link</a>" not in note_page
    assert ">Unlink from this homework</button>" not in details
    assert "made an assignment of its own" in page
    assert ">Link to this homework</button>" not in page
    assert unlinked.status_code == 409
    assert NOT_JOINED in unlinked.text
    assert after == before


# ------------------------------------------------------------------ who may


@pytest.mark.parametrize("press", ["link", "unlink"])
def test_the_other_persons_press_is_refused_with_what_was_typed_kept(
    press: str, tmp_path: pathlib.Path
) -> None:
    """A parent's press on her tree and her press on the family's are refused with the
    search words and the choice kept, before the note's name is read; the family's page
    itself stays the gate's to refuse; nothing is written."""
    app = create_app(signed_in_household(tmp_path))
    with TestClient(app, follow_redirects=False, headers=SAME_ORIGIN) as client:
        client.post("/sign-in", data={"passphrase": HERS})
        name = save_note(client)
        log = on_record(client)
        data = (
            {
                "revision": "1",
                "target": log.assignment_id,
                "basis": "x" * 64,
                "q": "Typed <b>words</b>",
            }
            if press == "link"
            else {"revision": "1", "from": log.assignment_id}
        )
        family_action = (note_link_action if press == "link" else note_unlink_action)(
            name, family=True
        )
        own_action = (note_link_action if press == "link" else note_unlink_action)(name)
        before = rows(client)
        hers_on_family = client.post(family_action, data=data, headers=PAGE_HEADERS)
        family_page = client.get(note_search_href(name, family=True), headers=PAGE_HEADERS)
        client.post("/sign-out")
        client.post("/sign-in", data={"passphrase": THEIRS})
        parents_on_hers = client.post(own_action, data=data, headers=PAGE_HEADERS)
        no_note = client.post(
            own_action.replace(name, "not-a-note"), data=data, headers=PAGE_HEADERS
        )
        parents_page = client.get(note_search_href(name, family=True), headers=PAGE_HEADERS)
        after = rows(client)

    assert hers_on_family.status_code == 403
    assert escape(NOT_A_PARENTS_PRESS) in hers_on_family.text
    assert family_page.status_code == 403
    assert "<h1>This page is for a parent</h1>" in family_page.text
    assert parents_on_hers.status_code == 403
    assert escape(NOT_HERS_TO_UPDATE) in parents_on_hers.text
    assert no_note.status_code == 403
    if press == "link":
        assert "Typed &lt;b&gt;words&lt;/b&gt;" in parents_on_hers.text
        assert "Typed &lt;b&gt;words&lt;/b&gt;" in no_note.text
    assert parents_page.status_code == 200
    assert "What she wrote" in parents_page.text
    assert after == before


def test_a_parent_joins_and_unlinks_through_the_familys_tree(tmp_path: pathlib.Path) -> None:
    app = create_app(signed_in_household(tmp_path))
    with TestClient(app, follow_redirects=False, headers=SAME_ORIGIN) as client:
        client.post("/sign-in", data={"passphrase": HERS})
        name = save_note(client, due_date="2026-08-21")
        client.post("/sign-out")
        client.post("/sign-in", data={"passphrase": THEIRS})
        log = on_record(client)
        page = search(client, name, "reading", family=True)
        press = press_of(page, name, log.assignment_id, family=True)
        answer = client.post(note_link_action(name, family=True), data=press)
        landed = client.get(answer.headers["location"], headers=PAGE_HEADERS).text
        history = state_of(client).project_state.capture_history(name)
        details = client.get(note_add_href(name, family=True), headers=PAGE_HEADERS).text
        unlinked = client.post(
            note_unlink_action(name, family=True), data=unlink_press(details, name, family=True)
        )
        after = state_of(client).project_state.capture_history(name)

    assert "What she wrote" in page
    assert answer.status_code == 303
    assert escape(JOINED_TO_HOMEWORK) in landed
    assert history[-1].authored_by == "parent"
    assert f'action="{note_unlink_action(name, family=True)}"' in details
    assert unlinked.status_code == 303
    assert (after[-1].operation, after[-1].authored_by) == ("unlink", "parent")


def test_reading_the_search_page_and_asking_for_it_with_head_write_nothing() -> None:
    with browser() as client:
        name = save_note(client)
        on_record(client)
        before = rows(client)
        head = client.head(note_search_href(name, q="reading"))
        head_of_the_note = client.head(note_href(name))
        got = client.get(note_search_href(name, q="reading"))
        no_note = client.get(note_search_href("not-a-note", q="reading"), headers=PAGE_HEADERS)
        after = rows(client)

    assert head.status_code == head_of_the_note.status_code
    assert got.status_code == 200
    assert no_note.status_code == 404
    assert after == before
    assert 'name="q"' in got.text


# ------------------------------------------------------------------ first review round


def test_a_parent_sees_the_corrections_on_the_note_page_through_the_familys_tree(
    tmp_path: pathlib.Path,
) -> None:
    """A parent reading a joined note's page gets Change link and Unlink, each through the
    family's tree, and the unlink press from that page is a parent's."""
    app = create_app(signed_in_household(tmp_path))
    with TestClient(app, follow_redirects=False, headers=SAME_ORIGIN) as client:
        client.post("/sign-in", data={"passphrase": HERS})
        name = save_note(client, due_date="2026-08-21")
        log = on_record(client)
        joined(client, name, log)
        client.post("/sign-out")
        client.post("/sign-in", data={"passphrase": THEIRS})
        note_page = client.get(note_href(name), headers=PAGE_HEADERS).text
        answer = client.post(
            note_unlink_action(name, family=True),
            data=unlink_press(note_page, name, family=True),
        )
        history = state_of(client).project_state.capture_history(name)

    assert f'<a href="{note_search_href(name, family=True)}">Change link</a>' in note_page
    assert f'action="{note_unlink_action(name, family=True)}"' in note_page
    assert answer.status_code == 303
    assert (history[-1].operation, history[-1].authored_by) == ("unlink", "parent")


def test_choosing_the_homework_the_note_is_joined_to_now_changes_nothing() -> None:
    """The search shows the homework the note is joined to with no press on it, and a press
    that names it anyway writes nothing: no unlink, no link, no claim withdrawn."""
    with browser() as client:
        name = save_note(client, due_date="2026-08-21")
        log = on_record(client)
        joined(client, name, log)
        page = search(client, name, "reading")
        row = page.split(f'id="found-{log.assignment_id}"')[1].split("</li>")[0]
        store = state_of(client).project_state
        basis = candidate_basis(readings_for(store, [log]))
        before = rows(client)
        answer = client.post(
            note_link_action(name),
            data={
                "revision": "2",
                "target": log.assignment_id,
                "basis": basis,
                "from": log.assignment_id,
                "q": "reading",
                "page": "1",
            },
        )
        landed = client.get(answer.headers["location"]).text
        after = rows(client)

    assert ">Move the link here</button>" not in row
    assert "joined to this homework now" in row
    assert answer.status_code == 303
    assert escape(ALREADY_ADDED) in landed
    assert after == before


def test_the_other_persons_press_on_a_note_that_cannot_be_read_or_is_gone_keeps_the_refusal(
    tmp_path: pathlib.Path,
) -> None:
    """Who pressed is settled first, whatever became of the note: the refusal is the one
    about who pressed, with the words kept, not a sentence about the note's row."""
    app = create_app(signed_in_household(tmp_path))
    with TestClient(app, follow_redirects=False, headers=SAME_ORIGIN) as client:
        client.post("/sign-in", data={"passphrase": HERS})
        name = save_note(client)
        gone_name = save_note(client, text="Another note, soon gone")
        log = on_record(client)
        store = state_of(client).project_state
        store._connection.execute(
            "UPDATE homework_captures SET archived = 2 WHERE capture_id = ?", (name,)
        )
        store._connection.execute(
            "DELETE FROM homework_captures WHERE capture_id = ?", (gone_name,)
        )
        store._connection.commit()
        client.post("/sign-out")
        client.post("/sign-in", data={"passphrase": THEIRS})
        data = {
            "revision": "1",
            "target": log.assignment_id,
            "basis": "x" * 64,
            "q": "Typed <b>words</b>",
        }
        unreadable = client.post(note_link_action(name), data=data, headers=PAGE_HEADERS)
        gone = client.post(note_link_action(gone_name), data=data, headers=PAGE_HEADERS)

    for answer in (unreadable, gone):
        assert answer.status_code == 403
        assert escape(NOT_HERS_TO_UPDATE) in answer.text
        assert "Typed &lt;b&gt;words&lt;/b&gt;" in answer.text
        assert escape(NOTE_UNREADABLE) not in answer.text
        assert escape(NOTE_GONE) not in answer.text


def test_homework_that_changed_so_the_words_no_longer_find_it_is_still_shown_as_it_stands() -> None:
    """The row the press was held to is shown as it stands now, with a fresh press, even
    when the search words find it no more."""
    with browser() as client:
        name = save_note(client)
        log = on_record(client)
        press = press_of(search(client, name, "reading"), name, log.assignment_id)
        store = state_of(client).project_state
        store._connection.execute(
            "UPDATE assignments SET title = 'Novel study' WHERE assignment_id = ?",
            (log.assignment_id,),
        )
        store._connection.commit()
        answer = client.post(note_link_action(name), data=press)
        shown = answer.text.split(f'id="chosen-{log.assignment_id}"')[1].split("</li>")[0]
        fresh = whole_form(shown, note_link_action(name))

    assert answer.status_code == 409
    assert HOMEWORK_CHANGED in answer.text
    assert "Novel study" in shown
    assert 'value="reading"' in answer.text
    assert (fresh["target"], fresh["revision"]) == (log.assignment_id, press["revision"])
    assert fresh["basis"] != press["basis"]


def test_a_note_joined_to_homework_that_left_the_record_still_offers_the_corrections() -> None:
    with browser() as client:
        name = save_note(client, due_date="2026-08-21")
        log = on_record(client)
        joined(client, name, log)
        store = state_of(client).project_state
        store._connection.execute(
            "DELETE FROM assignments WHERE assignment_id = ?", (log.assignment_id,)
        )
        store._connection.commit()
        note_page = client.get(note_href(name)).text
        page = search(client, name, "reading")
        answer = client.post(note_unlink_action(name), data=unlink_press(note_page, name))
        waiting = client.get(note_href(name)).text

    assert ">Change link</a>" in note_page
    assert f'action="{note_unlink_action(name)}"' in note_page
    assert "not on record now" in page
    assert answer.status_code == 303
    assert ">Link to homework already here</a>" in waiting


# ------------------------------------------------------------------ second review round


def change_note(client: TestClient, name: str, step: str, **typed: str) -> None:
    """Her edit of the note's words, class, or day, the rest as they stand, or her archiving
    it."""
    page = client.get(note_href(name, edit="1") if step == "edit" else note_href(name)).text
    action = note_action(name, step)
    held = state_of(client).project_state.capture(name)
    assert held is not None
    standing = (
        {
            "text": held.text,
            "course": held.course or "",
            "due_date": "" if held.due_date is None else held.due_date.isoformat(),
        }
        if step == "edit"
        else {}
    )
    answer = client.post(
        action, data={**form_fields(page, action), **standing, **typed}, headers=PAGE_HEADERS
    )
    assert answer.status_code == 303, answer.text[:300]


@pytest.mark.parametrize("fault", ["history", "assignments"])
@pytest.mark.parametrize("family", [False, True])
def test_an_unlink_refused_and_then_unreadable_lands_on_the_page_that_reads_no_store(
    fault: str, family: bool, tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The write is tried once and refused; the page that says so reads the record once
    and fails; the answer is then the page that reads no store, 500, one focused alert, the
    homework the form named shown as the link the form showed, and nothing changed."""
    app = create_app(signed_in_household(tmp_path))
    with TestClient(app, follow_redirects=False, headers=SAME_ORIGIN) as client:
        client.post("/sign-in", data={"passphrase": HERS})
        name = save_note(client, due_date="2026-08-21")
        if family:
            client.post("/sign-out")
            client.post("/sign-in", data={"passphrase": THEIRS})
        log = on_record(client)
        page = search(client, name, "reading", family=family)
        pressed = client.post(
            note_link_action(name, family=family),
            data=press_of(page, name, log.assignment_id, family=family),
        )
        assert pressed.status_code == 303
        details = client.get(note_add_href(name, family=family), headers=PAGE_HEADERS).text
        form = unlink_press(details, name, family=family)
        store = state_of(client).project_state
        attempts: list[int] = []
        reads: list[int] = []

        def refusing_write(*args: object, **kwargs: object) -> object:
            attempts.append(1)
            cause = "the file refused"
            raise CaptureNotSaved(name, RuntimeError(cause))

        def refusing_read(*args: object, **kwargs: object) -> object:
            reads.append(1)
            cause = "the file refused the read"
            raise sqlite3.OperationalError(cause)

        monkeypatch.setattr(store, "unlink_capture", refusing_write)
        monkeypatch.setattr(
            store,
            "sound_capture_history" if fault == "history" else "all_assignments",
            refusing_read,
        )
        before = rows(client)
        answer = client.post(
            note_unlink_action(name, family=family), data=form, headers=PAGE_HEADERS
        )
        after = rows(client)
        settled = not store._connection.in_transaction

    assert answer.status_code == 500
    assert (attempts, reads) == ([1], [1])
    assert settled
    assert answer.text.count(" autofocus") == 1
    assert 'id="problem-summary"' in answer.text
    assert escape(NOT_SAVED) in answer.text
    assert (
        f'Link shown on your form: <span class="authored-text">{log.assignment_id}</span>'
        in answer.text
    )
    assert after == before


@pytest.mark.parametrize("press", ["link", "unlink"])
@pytest.mark.parametrize("family", [False, True])
def test_a_press_on_a_name_that_is_no_note_keeps_the_words_and_the_choice(
    press: str, family: bool, tmp_path: pathlib.Path
) -> None:
    app = create_app(signed_in_household(tmp_path))
    with TestClient(app, follow_redirects=False, headers=SAME_ORIGIN) as client:
        client.post("/sign-in", data={"passphrase": THEIRS if family else HERS})
        data = {"revision": "1", "from": "former-target"}
        if press == "link":
            data.update(q="what <b>I</b> searched", page="1", target="chosen-target", basis="abc")
        action = (note_link_action if press == "link" else note_unlink_action)(
            "not-a-note", family=family
        )
        before = rows(client)
        answer = client.post(action, data=data, headers=PAGE_HEADERS)
        after = rows(client)

    assert answer.status_code == 404
    assert answer.text.count(" autofocus") == 1
    assert (
        'Link shown on your form: <span class="authored-text">former-target</span>' in answer.text
    )
    if press == "link":
        assert "what &lt;b&gt;I&lt;/b&gt; searched" in answer.text
        assert "chosen-target" in answer.text
    assert after == before


@pytest.mark.parametrize("later", ["course", "due_date", "archive"])
def test_the_accepted_press_sent_again_after_the_note_changed_writes_nothing(later: str) -> None:
    with browser() as client:
        name = save_note(client, due_date="2026-08-21")
        log = on_record(client)
        payload = press_of(search(client, name, "reading"), name, log.assignment_id)
        assert client.post(note_link_action(name), data=payload).status_code == 303
        if later == "archive":
            change_note(client, name, "archive")
        else:
            change_note(
                client, name, "edit", **{later: "Humanities" if later == "course" else "2026-08-23"}
            )
        before = rows(client)
        again = client.post(note_link_action(name), data=payload)
        landed = client.get(again.headers["location"]).text
        after = rows(client)

    assert again.status_code == 303
    assert after == before
    assert 'id="note-result"' in landed
    assert escape(ALREADY_ADDED) in landed


@pytest.mark.parametrize("later", ["edit", "archive"])
def test_the_accepted_move_sent_again_after_the_note_changed_writes_nothing(later: str) -> None:
    with browser() as client:
        name = save_note(client, due_date="2026-08-21")
        log = on_record(client)
        other = on_record(
            client, course="Spanish", title="Vocabulary list, unit nine", due=date(2026, 8, 21)
        )
        joined(client, name, log)
        payload = press_of(search(client, name, "nine"), name, other.assignment_id)
        assert client.post(note_link_action(name), data=payload).status_code == 303
        if later == "edit":
            change_note(client, name, "edit", text="Later words")
        else:
            change_note(client, name, "archive")
        before = rows(client)
        again = client.post(note_link_action(name), data=payload)
        landed = client.get(again.headers["location"]).text
        after = rows(client)

    assert again.status_code == 303
    assert after == before
    assert escape(ALREADY_ADDED) in landed


def test_the_homework_chosen_is_shown_once_from_the_pages_own_reading(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The row the press was held to is read with the page, once, as it stands when the
    page is made: not the row the refusing transaction read, and not twice when the words
    still find it."""
    with browser() as client:
        name = save_note(client)
        log = on_record(client)
        payload = press_of(search(client, name, "reading"), name, log.assignment_id)
        store = state_of(client).project_state
        store.put_on_record([log.model_copy(update={"title": "First reading worksheet"})], {})
        original = store.sound_capture_history
        renamed: list[int] = []

        def rename_before_the_page(capture_id: str) -> object:
            found = original(capture_id)
            if not renamed:
                renamed.append(1)
                store.put_on_record(
                    [log.model_copy(update={"title": "Second reading worksheet"})], {}
                )
            return found

        monkeypatch.setattr(store, "sound_capture_history", rename_before_the_page)
        answer = client.post(note_link_action(name), data=payload)
        presses = [
            whole_form(part.split("</li>")[0], note_link_action(name))
            for part in answer.text.split("<li id=")[1:]
            if f'"found-{log.assignment_id}"' in part[:80]
            or f'"chosen-{log.assignment_id}"' in part[:80]
        ]

    assert answer.status_code == 409
    assert HOMEWORK_CHANGED in answer.text
    assert "Second reading worksheet" in answer.text
    assert "First reading worksheet" not in answer.text
    assert len(presses) == 1
    assert presses[0]["target"] == log.assignment_id
    assert presses[0]["basis"] != payload["basis"]
    assert "This is the homework chosen." in answer.text


def test_the_homework_chosen_that_left_the_record_is_said_so_with_no_press() -> None:
    with browser() as client:
        name = save_note(client)
        log = on_record(client)
        payload = press_of(search(client, name, "reading"), name, log.assignment_id)
        store = state_of(client).project_state
        store._connection.execute(
            "DELETE FROM assignments WHERE assignment_id = ?", (log.assignment_id,)
        )
        store._connection.commit()
        answer = client.post(note_link_action(name), data=payload)

    assert answer.status_code == 409
    assert HOMEWORK_GONE in answer.text
    assert 'id="search-chosen-gone"' in answer.text
    assert f'"chosen-{log.assignment_id}"' not in answer.text
    assert f'value="{log.assignment_id}"' not in answer.text


def test_the_search_control_follows_the_servers_rule_and_names_its_error() -> None:
    """No native limit, which counts differently from the server's; a refused query marks
    the field, describes it by the alert, and the alert links to the field; a refusal about
    the homework chosen marks no field."""
    with browser() as client:
        name = save_note(client)
        log = on_record(client)
        plain = search(client, name, "")
        wide = search(client, name, chr(0x1D49C) * 200)
        refused = client.get(note_search_href(name, q="Q" * 201), headers=PAGE_HEADERS)
        payload = press_of(search(client, name, "reading"), name, log.assignment_id)
        report(client, log.assignment_id, "done")
        stale = client.post(note_link_action(name), data=payload)

    field = plain.split('id="search-words"')[1].split(">")[0]
    assert "maxlength" not in field
    assert "Up to 200 characters." in plain
    assert "aria-invalid" not in field
    assert NOTHING_FOUND in wide
    assert QUERY_REFUSED not in wide
    assert refused.status_code == 200
    bad = refused.text.split('id="search-words"')[1].split(">")[0]
    assert 'aria-invalid="true"' in bad
    assert 'aria-describedby="search-words-hint search-problem"' in bad
    alert = refused.text.split('id="search-problem"')[1].split("</p>")[0]
    assert 'href="#search-words">Review your search words</a>' in alert
    assert refused.text.count(" autofocus") == 1
    assert stale.status_code == 409
    assert "aria-invalid" not in stale.text.split('id="search-words"')[1].split(">")[0]


def test_a_refused_query_stays_refused_whatever_the_page_number() -> None:
    with browser() as client:
        name = save_note(client)
        answer = client.get(note_search_href(name, q="Q" * 201, page="2"), headers=PAGE_HEADERS)

    assert answer.status_code == 200
    assert QUERY_REFUSED in answer.text
    assert NO_SUCH_PAGE not in answer.text


def test_the_search_comes_first_and_the_note_opens_from_a_disclosure() -> None:
    """The search field and its button come before the note's words and the explanations,
    which a native disclosure holds whole; an error stays outside the disclosure."""
    long_words = "A long note. " * 37
    with browser() as client:
        name = save_note(client, text=long_words.strip())
        page = search(client, name, "")
        refused = client.get(note_search_href(name, q="Q" * 201), headers=PAGE_HEADERS).text

    assert page.index('id="search-words"') < page.index("<details")
    assert page.index(">Search</button>") < page.index("<details")
    assert "<summary>Read this homework note</summary>" in page
    assert page.index("<summary>Read this homework note</summary>") < page.index(
        escape(long_words.strip())
    )
    assert "Linking the homework note saved" in page.split("<details")[0]
    assert refused.index('id="search-problem"') < refused.index("<details")


# ------------------------------------------------------------------ third review round


@pytest.mark.parametrize("family", [False, True])
def test_a_link_sent_again_after_a_separate_unlink_is_that_press_and_an_old_move_is_not(
    family: bool, tmp_path: pathlib.Path
) -> None:
    app = create_app(signed_in_household(tmp_path))
    with TestClient(app, follow_redirects=False, headers=SAME_ORIGIN) as client:
        client.post("/sign-in", data={"passphrase": HERS})
        name = save_note(client, due_date="2026-08-21")
        if family:
            client.post("/sign-out")
            client.post("/sign-in", data={"passphrase": THEIRS})
        log = on_record(client)
        other = on_record(
            client, course="Spanish", title="Vocabulary list, unit nine", due=date(2026, 8, 21)
        )
        first = press_of(
            search(client, name, "reading", family=family), name, log.assignment_id, family=family
        )
        assert client.post(note_link_action(name, family=family), data=first).status_code == 303
        old_move = press_of(
            search(client, name, "nine", family=family), name, other.assignment_id, family=family
        )
        assert old_move["from"] == log.assignment_id
        details = client.get(note_add_href(name, family=family), headers=PAGE_HEADERS).text
        unlinked = client.post(
            note_unlink_action(name, family=family), data=unlink_press(details, name, family=family)
        )
        assert unlinked.status_code == 303
        fresh = press_of(
            search(client, name, "nine", family=family), name, other.assignment_id, family=family
        )
        assert "from" not in fresh
        assert client.post(note_link_action(name, family=family), data=fresh).status_code == 303
        before = rows(client)
        again = client.post(note_link_action(name, family=family), data=fresh)
        landed = client.get(again.headers["location"], headers=PAGE_HEADERS).text
        stale_move = client.post(note_link_action(name, family=family), data=old_move)
        after = rows(client)

    assert again.status_code == 303
    assert escape(ALREADY_ADDED) in landed
    assert stale_move.status_code == 409
    assert after == before


@pytest.mark.parametrize("lost", ["missing", "unreadable"])
@pytest.mark.parametrize("family", [False, True])
def test_an_unlink_refused_for_a_note_missing_or_unreadable_keeps_the_homework_named(
    lost: str, family: bool, tmp_path: pathlib.Path
) -> None:
    app = create_app(signed_in_household(tmp_path))
    with TestClient(app, follow_redirects=False, headers=SAME_ORIGIN) as client:
        client.post("/sign-in", data={"passphrase": HERS})
        name = save_note(client, due_date="2026-08-21")
        if family:
            client.post("/sign-out")
            client.post("/sign-in", data={"passphrase": THEIRS})
        log = on_record(client)
        pressed = press_of(
            search(client, name, "reading", family=family), name, log.assignment_id, family=family
        )
        assert client.post(note_link_action(name, family=family), data=pressed).status_code == 303
        details = client.get(note_add_href(name, family=family), headers=PAGE_HEADERS).text
        form = unlink_press(details, name, family=family)
        connection = state_of(client).project_state._connection
        if lost == "missing":
            connection.execute("DELETE FROM capture_events WHERE capture_id = ?", (name,))
            connection.execute("DELETE FROM homework_captures WHERE capture_id = ?", (name,))
        else:
            connection.execute(
                "UPDATE capture_events SET operation = 'damaged' "
                "WHERE capture_id = ? AND revision = 1",
                (name,),
            )
        connection.commit()
        before = rows(client)
        answer = client.post(
            note_unlink_action(name, family=family), data=form, headers=PAGE_HEADERS
        )
        after = rows(client)

    assert answer.status_code == (404 if lost == "missing" else 500)
    assert answer.text.count(" autofocus") == 1
    assert 'id="problem-summary"' in answer.text
    shown = f'Link shown on your form: <span class="authored-text">{log.assignment_id}</span>'
    assert shown in answer.text
    assert after == before


@pytest.mark.parametrize("family", [False, True])
def test_a_stale_unlink_names_what_it_asked_beside_the_link_that_stands(
    family: bool, tmp_path: pathlib.Path
) -> None:
    """The unlink named A; the note was moved to B from elsewhere meanwhile. The refusal says
    the unlink was asked for A, that the note is linked to B now, and offers the unlink of
    B as a press of its own, described by the link that stands."""
    app = create_app(signed_in_household(tmp_path))
    with TestClient(app, follow_redirects=False, headers=SAME_ORIGIN) as client:
        client.post("/sign-in", data={"passphrase": HERS})
        name = save_note(client, due_date="2026-08-21")
        if family:
            client.post("/sign-out")
            client.post("/sign-in", data={"passphrase": THEIRS})
        log = on_record(client)
        other = on_record(
            client, course="Spanish", title="Vocabulary list, unit nine", due=date(2026, 8, 21)
        )
        pressed = press_of(
            search(client, name, "reading", family=family), name, log.assignment_id, family=family
        )
        assert client.post(note_link_action(name, family=family), data=pressed).status_code == 303
        details = client.get(note_add_href(name, family=family), headers=PAGE_HEADERS).text
        old = unlink_press(details, name, family=family)
        moved = press_of(
            search(client, name, "nine", family=family), name, other.assignment_id, family=family
        )
        assert client.post(note_link_action(name, family=family), data=moved).status_code == 303
        before = rows(client)
        refused = client.post(
            note_unlink_action(name, family=family), data=old, headers=PAGE_HEADERS
        )
        after = rows(client)
        current = whole_form(refused.text, note_unlink_action(name, family=family))

    assert refused.status_code == 409
    assert refused.text.count(" autofocus") == 1
    asked = f'Unlink requested for <span class="authored-text">{log.assignment_id}</span>'
    assert asked in refused.text
    assert "not the homework this note is linked to now" in refused.text
    linked = refused.text.split('id="linked-now"')[1].split("</p>")[0]
    assert "Vocabulary list, unit nine" in linked
    assert other.assignment_id in linked
    assert refused.text.index('id="linked-now"') < refused.text.index('name="from"')
    assert current["from"] == other.assignment_id
    button = refused.text.split(">Unlink from this homework</button>")[0].rsplit("<button", 1)[1]
    assert 'aria-describedby="linked-now"' in button
    assert after == before


@pytest.mark.parametrize("family", [False, True])
def test_the_other_persons_unlink_is_refused_where_the_press_lives(
    family: bool, tmp_path: pathlib.Path
) -> None:
    """The refusal of who pressed is said on the details page, the unlink press's own page,
    with the homework the form named kept; a name that is no note keeps it on the page that
    reads no store."""
    app = create_app(signed_in_household(tmp_path))
    with TestClient(app, follow_redirects=False, headers=SAME_ORIGIN) as client:
        client.post("/sign-in", data={"passphrase": HERS})
        name = save_note(client, due_date="2026-08-21")
        log = on_record(client)
        joined(client, name, log)
        client.post("/sign-out")
        client.post("/sign-in", data={"passphrase": HERS if family else THEIRS})
        data = {"revision": "2", "from": log.assignment_id}
        before = rows(client)
        refused = client.post(
            note_unlink_action(name, family=family), data=data, headers=PAGE_HEADERS
        )
        no_note = client.post(
            note_unlink_action("not-a-note", family=family), data=data, headers=PAGE_HEADERS
        )
        after = rows(client)

    assert refused.status_code == 403
    assert "Sign in as" in refused.text
    assert "<h1>Add a note to homework</h1>" in refused.text
    asked = f'Unlink requested for <span class="authored-text">{log.assignment_id}</span>'
    assert asked in refused.text
    assert refused.text.count(" autofocus") == 1
    assert no_note.status_code == 403
    shown = f'Link shown on your form: <span class="authored-text">{log.assignment_id}</span>'
    assert shown in no_note.text
    assert after == before


def test_the_search_page_reads_the_note_and_the_homework_in_one_reading(
    tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Another connection links the note and renames the homework between the page's read
    of the note and its read of the homework. The page is one reading: either it is held
    and the other connection is refused, or the page shows the note and the homework as
    they stood together; never the note waiting beside the renamed homework."""
    app = create_app(signed_in_household(tmp_path))
    with TestClient(app, follow_redirects=False, headers=SAME_ORIGIN) as client:
        client.post("/sign-in", data={"passphrase": HERS})
        name = save_note(client)
        log = on_record(client)
        state = state_of(client)
        store = state.project_state
        connection = sqlite3.connect(
            state.settings.database_path, timeout=0.5, check_same_thread=False
        )
        other = ProjectStateStore(connection, state.clock, tables=False)
        original = store.sound_capture_history
        outcomes: list[str] = []

        def read_then_another_writes(capture_id: str) -> object:
            found = original(capture_id)
            if outcomes:
                return found
            try:
                outcome = other.link_capture(
                    name,
                    target=log.assignment_id,
                    expected_revision=1,
                    basis=candidate_basis(readings_for(other, [log])),
                    shown=row_reader(other),
                    authored_by=STUDENT,
                    channel=SourceChannel.STUDENT_REPORT,
                    now=state.clock.now(),
                    today=state.clock.today(),
                )
                outcomes.append(type(outcome).__name__)
                other.put_on_record([log.model_copy(update={"title": "Renamed reading log"})], {})
            except CaptureNotSaved as refused:
                outcomes.append(f"refused: {refused.__cause__}")
            return found

        monkeypatch.setattr(store, "sound_capture_history", read_then_another_writes)
        try:
            page = client.get(note_search_href(name, q="reading"), headers=PAGE_HEADERS)
        finally:
            connection.close()

    assert page.status_code == 200
    assert outcomes
    assert all("locked" in item.lower() for item in outcomes if item.startswith("refused"))
    waiting = "not in homework yet" in page.text
    renamed = "Renamed reading log" in page.text
    assert not (waiting and renamed), outcomes


def test_the_search_comes_first_when_the_note_is_linked_to_long_named_homework() -> None:
    with browser() as client:
        name = save_note(client, due_date="2026-08-21")
        long_named = on_record(client, course="C" * 60, title="Worksheet " + "T" * 189)
        pressed = press_of(search(client, name, "worksheet"), name, long_named.assignment_id)
        assert client.post(note_link_action(name), data=pressed).status_code == 303
        page = search(client, name, "")

    standing = page.split("Linking the homework note saved")[1].split("</p>")[0]
    assert "already linked to homework" in standing
    assert "T" * 189 not in standing
    assert page.index('id="search-words"') < page.index('id="search-linked-now"')
    assert page.index(">Search</button>") < page.index('id="search-linked-now"')
    linked = page.split('id="search-linked-now"')[1].split("</p>")[0]
    assert "T" * 189 in linked
    assert "moves this link" in linked
    assert page.index('id="search-linked-now"') < page.index("<details")
    assert page.count("moves this link") == 1


# ------------------------------------------------------------------ fourth review round


@pytest.mark.parametrize("left_out", ["q", "page"])
def test_a_link_press_without_its_words_or_its_page_is_not_whole(left_out: str) -> None:
    """Every link press the page makes carries the search words and the page; a form
    without them is one this page never made, and writes nothing."""
    with browser() as client:
        name = save_note(client, due_date="2026-08-21")
        log = on_record(client)
        payload = press_of(search(client, name, "reading"), name, log.assignment_id)
        del payload[left_out]
        before = rows(client)
        answer = client.post(note_link_action(name), data=payload, headers=PAGE_HEADERS)
        after = rows(client)

    assert answer.status_code == 422
    assert escape(BAD_FORM) in answer.text
    assert after == before


@pytest.mark.parametrize("basis", ["abc", "X" * 64, "0" * 63, "0" * 65])
def test_a_link_press_whose_fingerprint_is_not_as_written_is_not_whole(basis: str) -> None:
    """The fingerprint is sixty-four hexadecimal digits as the page writes it; anything else
    is a form this page never made, refused as not whole, never read as homework changed."""
    with browser() as client:
        name = save_note(client, due_date="2026-08-21")
        log = on_record(client)
        payload = {
            **press_of(search(client, name, "reading"), name, log.assignment_id),
            "basis": basis,
        }
        before = rows(client)
        answer = client.post(note_link_action(name), data=payload, headers=PAGE_HEADERS)
        after = rows(client)

    assert answer.status_code == 422
    assert escape(BAD_FORM) in answer.text
    assert escape(HOMEWORK_CHANGED) not in answer.text
    assert after == before


@pytest.mark.parametrize("turn", ["gone after changed", "back after gone"])
def test_the_refusal_names_the_homework_chosen_as_the_page_finds_it(
    turn: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The store refuses on what it read; the page reads again. Homework that changed and
    then left the record is said gone, and homework that was gone and is back is shown as
    it stands now, with a fresh press: the sentence and the rows come from one reading."""
    with browser() as client:
        name = save_note(client, due_date="2026-08-21")
        log = on_record(client)
        payload = press_of(search(client, name, "reading"), name, log.assignment_id)
        store = state_of(client).project_state
        if turn == "gone after changed":
            store.put_on_record(
                [log.model_copy(update={"title": "Summer reading log, week two"})], {}
            )
        else:
            store._connection.execute(
                "DELETE FROM assignments WHERE assignment_id = ?", (log.assignment_id,)
            )
            store._connection.commit()
        original = cast("Callable[..., object]", store.link_capture)

        def refuse_then_turn(*args: object, **kwargs: object) -> object:
            outcome = original(*args, **kwargs)
            if turn == "gone after changed":
                assert isinstance(outcome, CandidatesChanged)
                store._connection.execute(
                    "DELETE FROM assignments WHERE assignment_id = ?", (log.assignment_id,)
                )
                store._connection.commit()
            else:
                assert isinstance(outcome, HomeworkGone)
                store.put_on_record([log], {})
            return outcome

        monkeypatch.setattr(store, "link_capture", refuse_then_turn)
        before = {name: rows(client)[name] for name in ("homework_captures", "capture_events")}
        answer = client.post(note_link_action(name), data=payload, headers=PAGE_HEADERS)
        after = {name: rows(client)[name] for name in ("homework_captures", "capture_events")}

    assert answer.status_code == 409
    assert answer.text.count(" autofocus") == 1
    assert after == before
    if turn == "gone after changed":
        assert escape(HOMEWORK_GONE) in answer.text
        assert 'id="search-chosen-gone"' in answer.text
        assert escape(HOMEWORK_CHANGED) not in answer.text
        assert "This is the homework chosen." not in answer.text
    else:
        assert escape(HOMEWORK_CHANGED) in answer.text
        assert escape(HOMEWORK_GONE) not in answer.text
        row = answer.text.split(f'id="found-{log.assignment_id}"')[1].split("</li>")[0]
        assert "This is the homework chosen." in row
        assert ">Link to this homework</button>" in row


@pytest.mark.parametrize("family", [False, True])
@pytest.mark.parametrize("now", ["unlinked", "moved", "archived"])
def test_a_stale_move_names_what_it_asked_apart_from_the_link_that_stands(
    now: str, family: bool
) -> None:
    """A move from A to B kept on an old page, sent after the note left A from elsewhere:
    refused, nothing written, and the page says the move asked to leave A for B apart from
    where the note stands now and from any fresh press, whose fields are the record's."""
    with browser() as client:
        name = save_note(client, due_date="2026-08-21")
        log = on_record(client)
        other = on_record(
            client, course="Spanish", title="Vocabulary list, unit nine", due=date(2026, 8, 21)
        )
        joined(client, name, log)
        stale = press_of(
            search(client, name, "nine", family=family), name, other.assignment_id, family=family
        )
        assert stale["from"] == log.assignment_id
        if now == "moved":
            third = on_record(
                client, course="Science", title="Lab report, unit nine", due=date(2026, 8, 22)
            )
            moved = press_of(
                search(client, name, "lab", family=family), name, third.assignment_id, family=family
            )
            assert client.post(note_link_action(name, family=family), data=moved).status_code == 303
        else:
            details = client.get(note_add_href(name, family=family), headers=PAGE_HEADERS).text
            unlinked = client.post(
                note_unlink_action(name, family=family),
                data=unlink_press(details, name, family=family),
            )
            assert unlinked.status_code == 303
            if now == "archived":
                change_note(client, name, "archive")
        standing = state_of(client).project_state.capture(name)
        assert standing is not None
        before = rows(client)
        refused = client.post(
            note_link_action(name, family=family), data=stale, headers=PAGE_HEADERS
        )
        after = rows(client)

    assert refused.status_code == 409
    assert after == before
    assert refused.text.count(" autofocus") == 1
    request = refused.text.split('id="search-request"')[1].split("</p>")[0]
    assert f'from <span class="authored-text">{log.assignment_id}</span>' in request
    assert f'to <span class="authored-text">{other.assignment_id}</span>' in request
    assert "This move was not saved." in request
    row = refused.text.split(f'id="found-{other.assignment_id}"')[1].split("</li>")[0]
    if now == "moved":
        linked = refused.text.split('id="search-linked-now"')[1].split("</p>")[0]
        assert "Lab report, unit nine" in linked
        assert "This note is not linked to homework now." not in refused.text
        fresh = whole_form(row, note_link_action(name, family=family))
        assert (fresh["from"], fresh["revision"]) == (third.assignment_id, str(standing.revision))
    else:
        assert "This note is not linked to homework now." in refused.text
        assert 'id="search-linked-now"' not in refused.text
        if now == "unlinked":
            fresh = whole_form(row, note_link_action(name, family=family))
            assert "from" not in fresh
            assert fresh["revision"] == str(standing.revision)
        else:
            assert "<form" not in row


@pytest.mark.parametrize("family", [False, True])
@pytest.mark.parametrize("later", ["edit", "archive"])
def test_a_stale_unlink_names_what_it_asked_when_the_note_is_linked_to_nothing_now(
    later: str, family: bool
) -> None:
    """An unlink of A kept on an old page, sent after the note left A and was edited or put
    away from elsewhere: refused, nothing written, the page names A as what was asked and
    says the note is linked to nothing now, and offers no unlink press."""
    with browser() as client:
        name = save_note(client, due_date="2026-08-21")
        log = on_record(client)
        joined(client, name, log)
        details = client.get(note_add_href(name, family=family), headers=PAGE_HEADERS).text
        stale = unlink_press(details, name, family=family)
        assert client.post(note_unlink_action(name, family=family), data=stale).status_code == 303
        if later == "edit":
            change_note(client, name, "edit", text="Edited after leaving that homework")
        else:
            change_note(client, name, "archive")
        before = rows(client)
        refused = client.post(
            note_unlink_action(name, family=family), data=stale, headers=PAGE_HEADERS
        )
        after = rows(client)

    assert refused.status_code == 409
    assert after == before
    assert refused.text.count(" autofocus") == 1
    asked = (
        f'Unlink requested for <span class="authored-text">{log.assignment_id}</span>. '
        "Nothing was changed."
    )
    assert asked in refused.text
    assert "This note is not linked to homework now." in refused.text
    assert "not the homework this note is linked to now" not in refused.text
    assert ">Unlink from this homework</button>" not in refused.text
    assert note_unlink_action(name, family=family) not in refused.text.split("<h1>")[1]
