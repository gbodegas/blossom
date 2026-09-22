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
from datetime import date

import pytest
from fastapi.testclient import TestClient
from markupsafe import escape

from blossom import intake
from blossom.app import create_app
from blossom.captures import CaptureNotSaved, derived_assignment_id
from blossom.homework_search import PAGE_SIZE
from blossom.reconciliation import SourceChannel
from blossom.routes.captures import JOINED_TO_HOMEWORK, LINK_CHANGED, NOTE_CHANGED, UNLINKED
from blossom.routes.navigation import (
    NEW_NOTE_PAGE,
    NOTE_ACTIONS,
    NOTES_PAGE,
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
from blossom.stores.project_state import Assignment
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

    assert "Joined now to" in page
    assert "Summer reading log" in page.split("Joined now to")[1].split("</p>")[0]
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
