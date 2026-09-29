"""Her homework notes on the pages that already exist: her week, the family page, and a
request for help that is about a note. What a note never does is here too: it reaches no
model, makes no plan stale, and costs a page no read per note.
"""

import pathlib
import re
import sqlite3
from datetime import timedelta
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient
from markupsafe import escape

from blossom.app import create_app
from blossom.captures import new_capture_id
from blossom.routes import captures as note_routes
from blossom.routes.captures import (
    HELP_NOT_ASKED,
    NOTE_ASKED,
    NOTE_CANNOT_BE_READ,
    NOTE_GONE,
    NOTE_NOT_SAVED,
    NOTE_UNREADABLE,
)
from blossom.routes.navigation import (
    NEW_NOTE_PAGE,
    NOTE_ACTIONS,
    NOTES_PAGE,
    note_action,
    note_help_href,
    note_href,
)
from blossom.routes.parent import ASSIGNMENTS_CHANGED as THEIR_ASSIGNMENTS_CHANGED
from blossom.routes.runs import plan_graphs
from blossom.routes.student import (
    ASSIGNMENTS_CHANGED,
    HELP_FORM_NOT_WHOLE,
    NOT_HERS_TO_ASK,
    NOT_HERS_TO_UPDATE,
)
from blossom.stores.help_requests import UnknownCaptureReference
from blossom.stores.project_state import ProjectStateStore
from tests.support import (
    FIXTURE_WEEK,
    HER_PAGE,
    HERS,
    PAGE_HEADERS,
    PLAN_DATE,
    SAME_ORIGIN,
    THEIRS,
    Answer,
    Scripted,
    accepting,
    browser,
    dissent,
    fixture_week_plan,
    form_fields,
    report,
    scripted_graphs,
    signed_in_household,
    state_of,
    whole_form,
)

FAMILY = "/parent"
WORDS = "Geometry questions 4-8, heard from a classmate"


def save_note(client: TestClient, text: str = WORDS, **typed: str) -> str:
    fields = form_fields(client.get(NEW_NOTE_PAGE).text, NOTE_ACTIONS)
    answer = client.post(
        NOTE_ACTIONS,
        data={**fields, "text": text, "course": typed.get("course", ""), "due_date": ""},
        headers=PAGE_HEADERS,
    )
    assert answer.status_code == 303, answer.text
    return fields["capture_id"]


def change_note(client: TestClient, name: str, step: str, **typed: str) -> Answer:
    page = client.get(note_href(name, edit="1") if step == "edit" else note_href(name)).text
    action = note_action(name, step)
    return client.post(action, data={**form_fields(page, action), **typed}, headers=PAGE_HEADERS)


def ask_about(client: TestClient, name: str, question: str = "") -> Answer:
    """One press from a fresh form: its own request id and her question."""
    return client.post(
        note_action(name, "ask-for-help"),
        data={"note": question, "request_id": uuid4().hex},
        headers=PAGE_HEADERS,
    )


def notes_section(page: str) -> str:
    start = page.index('id="homework-notes"')
    return page[start : page.index("</section>", start)]


def statements(store: ProjectStateStore, client: TestClient, where: str) -> list[str]:
    seen: list[str] = []
    store._connection.set_trace_callback(seen.append)
    try:
        assert client.get(where).status_code == 200
    finally:
        store._connection.set_trace_callback(None)
    return seen


# ------------------------------------------------------------------ her week


def test_her_week_offers_the_way_in_and_shows_the_oldest_notes_outside_the_week() -> None:
    with browser() as client:
        empty = client.get(HER_PAGE, params={"week": FIXTURE_WEEK}).text
        names = [save_note(client, f"note number {index}") for index in range(4)]
        change_note(
            client, names[0], "edit", text="note number 0, edited last", course="", due_date=""
        )
        week = client.get(HER_PAGE, params={"week": FIXTURE_WEEK}).text
        change_note(client, names[1], "archive")
        fewer = client.get(HER_PAGE, params={"week": FIXTURE_WEEK}).text

    assert f'href="{NEW_NOTE_PAGE}">Write down homework</a>' in empty
    assert f'<a href="{NOTES_PAGE}">Homework notes</a>' in empty
    assert "Homework notes (0)" not in empty
    assert 'id="homework-notes"' not in empty
    assert f'<a href="{NOTES_PAGE}">Homework notes (4)</a>' in week
    shown = notes_section(week)
    assert "Homework notes (4)" in shown
    assert [f"note number {index}" in shown for index in range(4)] == [True, True, True, False]
    assert shown.index("note number 0, edited last") < shown.index("note number 1")
    assert "View all 4 homework notes" in shown
    assert "It is not in a plan yet." in shown
    assert shown.count("Add the class and title to put this in a plan.") == 3
    assert not [words for words in ("Ready to add", "Link to", "Search homework") if words in shown]
    assert week.index('id="today"') < week.index('id="homework-notes"')
    assert week.index('id="homework-notes"') < week.index('class="list-heading"')
    assert "Homework notes (3)" in notes_section(fewer)
    assert "View all" not in notes_section(fewer)


def test_with_every_assignment_done_and_an_empty_week_the_notes_are_still_there(
    tmp_path: pathlib.Path,
) -> None:
    with browser(key=True) as client:
        store = state_of(client).project_state
        for item in store.all_assignments():
            report(client, item.assignment_id, "done")
        save_note(client)
        done = client.get(HER_PAGE).text
    empty_record = str(tmp_path / "empty.sqlite3")
    with browser(BLOSSOM_FIXTURE_PATH="", BLOSSOM_DATABASE_PATH=empty_record) as client:
        nothing_due = client.get(HER_PAGE).text
        save_note(client)
        with_a_note = client.get(HER_PAGE).text

    assert 'action="/student/actions/plan"' not in done
    assert "Homework notes (1)" in notes_section(done)
    assert done.index('id="homework-notes"') < done.index("Reported done (")
    assert "No assignments are recorded as due" in nothing_due
    assert f'href="{NEW_NOTE_PAGE}">Write down homework</a>' in nothing_due
    assert "Homework notes (1)" in notes_section(with_a_note)


@pytest.mark.parametrize("where", [HER_PAGE, NOTES_PAGE, FAMILY])
def test_a_page_costs_the_same_statements_however_many_notes_and_requests_there_are(
    where: str, tmp_path: pathlib.Path
) -> None:
    costs = []
    for count in (1, 20, 200):
        record = str(tmp_path / f"record-{count}.sqlite3")
        with browser(BLOSSOM_DATABASE_PATH=record) as client:
            state = state_of(client)
            names = [new_capture_id() for _ in range(count)]
            for index, name in enumerate(names):
                created = client.post(
                    NOTE_ACTIONS,
                    data={
                        "capture_id": name,
                        "text": f"note {index}",
                        "course": "",
                        "due_date": "",
                    },
                    headers=PAGE_HEADERS,
                )
                assert created.status_code == 303
                state.help_requests.ask(PLAN_DATE, None, capture_id=name)
            costs.append(len(statements(state.project_state, client, where)))

    assert costs[0] == costs[1] == costs[2]
    # One of them is the one read of the school's instructions, however many there are.
    assert costs[0] <= 11


# -------------------------------------------------------------- the family page


def test_the_family_page_shows_what_she_added_and_offers_no_way_to_change_it(
    tmp_path: pathlib.Path,
) -> None:
    app = create_app(signed_in_household(tmp_path))
    with TestClient(app, follow_redirects=False, headers=SAME_ORIGIN) as client:
        client.post("/sign-in", data={"passphrase": HERS})
        name = save_note(client, "first <b>words</b>", course="Geometry")
        change_note(client, name, "edit", text="current words", course="Geometry", due_date="")
        put_away = save_note(client, "a note she put away")
        change_note(client, put_away, "archive")
        client.post("/sign-out")
        client.post("/sign-in", data={"passphrase": THEIRS})

        family = client.get(FAMILY).text

    start = family.index("<h2>Homework she added</h2>")
    section = family[start : family.index("</section>", start)]
    assert "current words" in section
    assert "first &lt;b&gt;words&lt;/b&gt;" in section
    assert "<b>words</b>" not in family
    assert "Geometry" in section
    assert f'href="{note_href(name)}"' in section
    assert "a note she put away" not in section
    assert "Archived homework notes" in section
    assert "<form" not in section
    assert "It is not an assignment" in section


# ------------------------------------------------------------ help about a note


def test_opening_the_help_page_sends_nothing_and_a_request_names_the_note(
    tmp_path: pathlib.Path,
) -> None:
    app = create_app(signed_in_household(tmp_path))
    with TestClient(app, follow_redirects=False, headers=SAME_ORIGIN) as client:
        client.post("/sign-in", data={"passphrase": HERS})
        help_store = state_of(client).help_requests
        name = save_note(client, "Geometry questions 4-8")
        opened = client.get(note_help_href(name))
        nothing_sent = help_store.open_requests()

        asked = ask_about(client, name, "which questions?")
        landed = client.get(asked.headers["location"]).text
        requests = help_store.open_requests()
        change_note(client, name, "edit", text="Geometry questions 4-9", course="", due_date="")
        hers = client.get(HER_PAGE).text
        change_note(client, name, "archive")
        client.post("/sign-out")
        client.post("/sign-in", data={"passphrase": THEIRS})
        theirs = client.get(FAMILY).text
        store = state_of(client).project_state
        store._connection.execute("UPDATE homework_captures SET due_date = 'next week'")
        store._connection.commit()
        unreadable = client.get(FAMILY).text

    assert opened.status_code == 200
    assert "Geometry questions 4-8" in opened.text
    assert nothing_sent == []
    assert asked.status_code == 303
    assert escape(NOTE_ASKED) in landed
    assert [(item.capture_id, item.note) for item in requests] == [(name, "which questions?")]
    assert "Geometry questions" not in (requests[0].note or "")
    assert "About your homework note, as it stands now" in hers
    assert "Geometry questions 4-9" in hers
    assert f'href="{note_href(name)}"' in hers
    assert "About her homework note, as it stands now" in theirs
    assert "Geometry questions 4-9" in theirs
    assert ">Open homework note</a>" in theirs
    assert "which questions?" in unreadable
    assert "Her homework note cannot be read right now." in unreadable


def test_a_request_about_a_name_that_is_no_note_is_refused_and_writes_nothing() -> None:
    with browser() as client:
        help_store = state_of(client).help_requests
        save_note(client)

        answers = [
            ask_about(client, new_capture_id(), "about nothing"),
            client.post(
                f"{NOTE_ACTIONS}/note-1/ask-for-help", data={"note": ""}, headers=PAGE_HEADERS
            ),
        ]
        extra = client.post(
            note_action(save_note(client, "another"), "ask-for-help"),
            data={"note": "", "capture_id": new_capture_id()},
            headers=PAGE_HEADERS,
        )
        sent = help_store.open_requests()

    assert [answer.status_code for answer in answers] == [404, 404]
    assert extra.status_code == 422
    assert sent == []


def test_a_request_read_without_a_page_still_says_the_note_it_is_about() -> None:
    """The endpoints that answer in JSON read the notes their requests name, in one statement
    for all of them, so a note that can be read is never said to be unavailable."""
    with browser() as client:
        state = state_of(client)
        names = [save_note(client, f"Note {number}") for number in range(3)]
        for name in names:
            assert ask_about(client, name, "which part?").status_code == 303
        client.post("/student/help-requests", json={"note": "about no note"})
        seen: list[str] = []
        state.project_state._connection.set_trace_callback(seen.append)
        hers = client.get("/student/help-requests")
        hers_cost = [line for line in seen if "homework_captures" in line]
        seen.clear()
        theirs = client.get("/parent/help-requests")
        theirs_cost = [line for line in seen if "homework_captures" in line]
        state.project_state._connection.set_trace_callback(None)
        first = next(item for item in theirs.json() if item["about_note"] is not None)
        taken_up = client.post(f"/parent/help-requests/{first['request_id']}/accept", json={})
        answered = client.post(f"/parent/help-requests/{first['request_id']}/resolve", json={})

    for listing in (hers.json(), theirs.json()):
        about = [item["about_note"] for item in listing]
        assert about.count(None) == 1
        said = {note["capture_id"]: note for note in about if note is not None}
        assert {name: note["text"] for name, note in said.items()} == {
            name: f"Note {number}" for number, name in enumerate(names)
        }
        assert not any(note["unavailable"] or note["archived"] for note in said.values())
    assert (len(hers_cost), len(theirs_cost)) == (1, 1)
    for answer in (taken_up, answered):
        assert answer.status_code == 200
        assert answer.json()["about_note"] == first["about_note"]
        assert answer.json()["about_note"]["unavailable"] is False


def test_a_question_that_is_too_long_links_to_its_field_and_the_field_points_back() -> None:
    with browser() as client:
        help_store = state_of(client).help_requests
        name = save_note(client)
        answer = ask_about(client, name, "q" * 501)
        sent = help_store.open_requests()

    alert = re.search(r'<p class="problem"[^>]*id="note-problem"[^>]*>.*?</p>', answer.text, re.S)
    field = re.search(r'<input\b[^>]*id="help-question"[^>]*>', answer.text)
    assert answer.status_code == 422
    assert alert is not None
    assert field is not None
    assert '<a href="#help-question">' in alert.group()
    assert answer.text.count(" autofocus") == 1
    assert 'aria-invalid="true"' in field.group()
    assert re.search(r'aria-describedby="note-problem help-question-hint"', field.group())
    assert sent == []


@pytest.mark.parametrize("what", ["unreadable", "gone", "read_failure", "reference"])
def test_a_request_for_help_that_is_refused_keeps_her_question_whatever_became_of_the_note(
    what: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Her question is read before the note is, so no refusal loses it: a note that cannot
    be read, one that left the record, a file that cannot be read, and a note that left
    between the read and the write. The page that answers reads no store and sends nothing."""
    question = "UNSAVED <strong>why</strong> \U0001f600"
    with browser() as client:
        state = state_of(client)
        store = state.project_state
        name = save_note(client)
        action = note_action(name, "ask-for-help")
        opened = whole_form(client.get(note_help_href(name)).text, action)
        if what == "unreadable":
            store._connection.execute(
                "UPDATE homework_captures SET attribution = 'no json' WHERE capture_id = ?", (name,)
            )
        elif what == "gone":
            store._connection.execute("DELETE FROM homework_captures WHERE capture_id = ?", (name,))
        store._connection.commit()
        tried: list[str] = []
        if what == "read_failure":

            def unread(*args: object, **kwargs: object) -> None:
                msg = "the file cannot be read"
                raise sqlite3.OperationalError(msg)

            monkeypatch.setattr(store, "capture", unread)
        if what == "reference":

            def left(*args: object, **kwargs: object) -> None:
                tried.append("write")
                raise UnknownCaptureReference(name)

            monkeypatch.setattr(state.help_requests, "ask_once", left)
        seen: list[str] = []
        store._connection.set_trace_callback(seen.append)
        answer = client.post(action, data={**opened, "note": question}, headers=PAGE_HEADERS)
        store._connection.set_trace_callback(None)
        monkeypatch.undo()
        sent = state.help_requests.open_requests()

    expected = {"unreadable": 500, "gone": 404, "read_failure": 500, "reference": 404}[what]
    assert answer.status_code == expected
    assert answer.text.count(" autofocus") == 1
    assert "UNSAVED &lt;strong&gt;why&lt;/strong&gt; \U0001f600" in answer.text
    assert "<strong>why</strong>" not in answer.text
    assert str(escape(NOTE_ASKED)) not in answer.text
    assert WORDS not in answer.text
    assert f'href="{NOTES_PAGE}"' in answer.text
    assert tried == (["write"] if what == "reference" else [])
    assert len([line for line in seen if "homework_captures" in line]) <= 1
    assert sent == []


def test_signed_in_the_same_endpoints_say_a_readable_note_is_readable_and_a_damaged_one_is_not(
    tmp_path: pathlib.Path,
) -> None:
    """As a household runs it: she asks signed in as the student, a parent reads and answers
    signed in as a parent. A note that reads is never said to be unavailable, and one that
    really cannot be read still is, so no constant answer passes."""
    app = create_app(signed_in_household(tmp_path))
    with TestClient(app, follow_redirects=False, headers=SAME_ORIGIN) as client:
        client.post("/sign-in", data={"passphrase": HERS})
        name = save_note(client)
        damaged = save_note(client, "A note that will not read")
        for about in (name, damaged):
            assert ask_about(client, about, "").status_code == 303
        store = state_of(client).project_state
        store._connection.execute(
            "UPDATE homework_captures SET attribution = 'no json' WHERE capture_id = ?", (damaged,)
        )
        store._connection.commit()
        hers = client.get("/student/help-requests")
        client.post("/sign-out")
        client.post("/sign-in", data={"passphrase": THEIRS})
        theirs = client.get("/parent/help-requests")
        readable = next(item for item in theirs.json() if item["about_note"]["capture_id"] == name)
        moved = [
            client.post(f"/parent/help-requests/{readable['request_id']}/{step}", json={})
            for step in ("accept", "resolve")
        ]

    expected = {
        name: {"capture_id": name, "text": WORDS, "archived": False, "unavailable": False},
        damaged: {"capture_id": damaged, "text": None, "archived": False, "unavailable": True},
    }
    for listing in (hers, theirs):
        assert listing.status_code == 200
        said = {item["about_note"]["capture_id"]: item["about_note"] for item in listing.json()}
        assert said == expected
    for answer in moved:
        assert answer.status_code == 200
        assert answer.json()["about_note"] == expected[name]


@pytest.mark.parametrize("whole", [True, False], ids=["whole", "no id"])
@pytest.mark.parametrize(
    "what",
    ["readable", "archived", "not an id", "no such note", "deleted", "unreadable"],
)
def test_a_parent_asking_about_a_note_is_refused_before_the_note_is_looked_up(
    what: str, whole: bool, tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A parent's press is answered 403 from the address alone, whatever became of the note."""
    app = create_app(signed_in_household(tmp_path))
    with TestClient(app, follow_redirects=False, headers=SAME_ORIGIN) as client:
        client.post("/sign-in", data={"passphrase": HERS})
        name = save_note(client)
        if what == "archived":
            assert change_note(client, name, "archive").status_code == 303
        client.post("/sign-out")
        client.post("/sign-in", data={"passphrase": THEIRS})
        state = state_of(client)
        store = state.project_state
        about = {"not an id": "note-1", "no such note": new_capture_id()}.get(what, name)
        if what == "deleted":
            store._connection.execute("DELETE FROM homework_captures WHERE capture_id = ?", (name,))
        elif what == "unreadable":
            store._connection.execute(
                "UPDATE homework_captures SET attribution = 'no json' WHERE capture_id = ?", (name,)
            )
        store._connection.commit()
        tried: list[str] = []

        def looked_up(*args: object, **kwargs: object) -> None:
            tried.append("lookup")
            msg = "a parent's press looked the note up"
            raise AssertionError(msg)

        monkeypatch.setattr(store, "sound_capture_history", looked_up)
        fields = {"note": "from a parent", "request_id": uuid4().hex}
        if not whole:
            del fields["request_id"]
        seen: list[str] = []
        store._connection.set_trace_callback(seen.append)
        answers = [
            client.post(note_action(about, "ask-for-help"), data=fields, headers=PAGE_HEADERS)
            for _ in range(2)
        ]
        store._connection.set_trace_callback(None)
        monkeypatch.undo()
        help_rows = [
            state.help_requests._connection.execute(statement).fetchone()[0]
            for statement in (
                "SELECT count(*) FROM help_requests",
                "SELECT count(*) FROM help_request_ids",
                "SELECT count(*) FROM notes_named_by_requests",
            )
        ]

    for answer in answers:
        assert answer.status_code == 403
        assert str(escape(NOT_HERS_TO_ASK)) in answer.text
        assert str(escape(NOT_HERS_TO_UPDATE)) not in answer.text
        assert (f'href="{note_href(about)}"' in answer.text) is (what != "not an id")
    assert [line for line in seen if "capture" in line] == []
    assert tried == []
    assert help_rows == [0, 0, 0]


@pytest.mark.parametrize(
    ("body", "kind"),
    [
        (
            b"--synthetic\r\nContent-Disposition: form-data\r\n\r\nno end",
            "multipart/form-data; boundary=synthetic",
        ),
        (b"note=from+a+parent&note=twice", "application/x-www-form-urlencoded"),
        (b"", "application/x-www-form-urlencoded"),
    ],
    ids=["broken multipart", "note twice", "empty"],
)
def test_a_parent_asking_about_a_note_is_refused_before_the_form_is_read(
    body: bytes, kind: str, tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    app = create_app(signed_in_household(tmp_path))
    with TestClient(app, follow_redirects=False, headers=SAME_ORIGIN) as client:
        client.post("/sign-in", data={"passphrase": HERS})
        name = save_note(client)
        client.post("/sign-out")
        client.post("/sign-in", data={"passphrase": THEIRS})

        async def unread(*args: object, **kwargs: object) -> None:
            msg = "a parent's form was read"
            raise AssertionError(msg)

        monkeypatch.setattr(note_routes, "fields_of", unread)
        refused = client.post(
            note_action(name, "ask-for-help"),
            content=body,
            headers={**PAGE_HEADERS, "Content-Type": kind},
        )
        monkeypatch.undo()
        held = help_held(client)

    assert refused.status_code == 403
    assert str(escape(NOT_HERS_TO_ASK)) in refused.text
    assert f'href="{note_href(name)}"' in refused.text
    assert "from a parent" not in refused.text
    assert held == ([], 0)


def own_row(page: str, *, family: bool, resolved: bool) -> str:
    """The one request's own row: its list item or its card, and nothing else of the page."""
    if resolved and family:
        summary = "Resolved in the last two weeks"
        rows = page.split(f"<summary>{summary}</summary>", 1)[1].split("</details>", 1)[0]
        return rows.split("<li", 1)[1].split("</li>", 1)[0]
    if family:
        return page.split('<article class="draft help-', 1)[1].split("</article>", 1)[0]
    return page.split('<ul class="help">', 1)[1].split("</li>", 1)[0]


@pytest.mark.parametrize("stage", ["asked", "taken up", "resolved"])
@pytest.mark.parametrize("became", ["as saved", "edited", "archived", "unavailable"])
def test_a_request_about_a_note_says_so_in_its_own_row_at_every_stage_on_both_pages(
    stage: str, became: str
) -> None:
    """Asked with no question, so the note is all that says what the request is about. The
    row itself is read, since the note being somewhere else on the page is not enough, and
    each page still costs the one statement for every note its requests name."""
    with browser() as client:
        state = state_of(client)
        store = state.project_state
        name = save_note(client)
        assert ask_about(client, name, "").status_code == 303
        asked = state.help_requests.open_requests()[0]
        if became == "edited":
            change_note(client, name, "edit", text="Edited words", course="", due_date="")
        elif became == "archived":
            change_note(client, name, "archive")
        elif became == "unavailable":
            store._connection.execute("UPDATE homework_captures SET attribution = 'no json'")
            store._connection.commit()
        if stage == "taken up":
            state.help_requests.accept(asked.request_id, None)
        elif stage == "resolved":
            state.help_requests.resolve(asked.request_id, "We talked")
        seen: list[str] = []
        store._connection.set_trace_callback(seen.append)
        pages = {False: client.get(HER_PAGE).text, True: client.get(FAMILY).text}
        store._connection.set_trace_callback(None)

    named = [line for line in seen if "json_each" in line and "homework_captures" in line]
    assert len(named) == 2
    for family, page in pages.items():
        row = own_row(page, family=family, resolved=stage == "resolved")
        whose = "her" if family else "your"
        if became == "unavailable":
            assert f"{whose.capitalize()} homework note cannot be read right now." in row
            assert "Open homework note" not in row
            continue
        words = "Edited words" if became == "edited" else WORDS
        assert f"About {whose} homework note, as it stands now" in row
        assert ("(archived)" in row) is (became == "archived")
        assert str(escape(words)) in row
        assert f'<a href="{note_href(name)}">Open homework note</a>' in row


@pytest.mark.parametrize(
    "where",
    ["accept", "resolve", "/student/help-requests", "/parent/help-requests", HER_PAGE, FAMILY],
)
def test_a_read_of_the_notes_that_fails_never_fails_an_answer_about_a_request(
    where: str, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """The notes a request names are context. A parent's accept or resolve has already been
    written when they are read, so that read must not be able to fail the answer: the note
    is said to be unavailable, the failure is logged, and the request stands as it was moved."""
    with browser() as client:
        state = state_of(client)
        name = save_note(client)
        assert ask_about(client, name, "").status_code == 303
        asked = state.help_requests.open_requests()[0]

        def unread(*args: object, **kwargs: object) -> None:
            msg = "the file cannot be read"
            raise sqlite3.OperationalError(msg)

        monkeypatch.setattr(state.project_state, "captures_named", unread)
        if where in ("accept", "resolve"):
            answer = client.post(f"/parent/help-requests/{asked.request_id}/{where}", json={})
        else:
            answer = client.get(where)
        monkeypatch.undo()
        stands = state.help_requests.get(asked.request_id)

    assert answer.status_code == 200
    assert any("could not be read" in record.getMessage() for record in caplog.records)
    assert stands is not None
    if where in ("accept", "resolve"):
        assert stands.state == {"accept": "accepted", "resolve": "resolved"}[where]
        assert answer.json()["about_note"] == {
            "capture_id": name,
            "text": None,
            "archived": False,
            "unavailable": True,
        }
    elif where.endswith("help-requests"):
        assert answer.json()[0]["about_note"]["unavailable"] is True
    else:
        whose = "Her" if where == FAMILY else "Your"
        assert f"{whose} homework note cannot be read right now." in answer.text


@pytest.mark.parametrize("readable", [0, 1, 2, 3, 4])
def test_a_note_that_cannot_be_read_is_counted_wherever_her_notes_are_counted(
    readable: int,
) -> None:
    """It is a note of hers, waiting like the others, so the link, the section, and the page
    of notes all count it, and say apart that it cannot be read."""
    with browser() as client:
        store = state_of(client).project_state
        for number in range(readable):
            save_note(client, f"Readable {number}")
        damaged = save_note(client, "Damaged")
        store._connection.execute(
            "UPDATE homework_captures SET attribution = 'no json' WHERE capture_id = ?", (damaged,)
        )
        store._connection.commit()
        week = client.get(HER_PAGE).text
        listed = client.get(NOTES_PAGE).text

    total = readable + 1
    assert f'<a href="{NOTES_PAGE}">Homework notes ({total})</a>' in week
    assert f"<h2>Homework notes ({total})</h2>" in notes_section(week)
    assert "1 homework note cannot be read right now." in notes_section(week)
    assert (f"View all {total} homework notes" in week) is (total > 3)
    assert ("View all" in notes_section(week)) is (total > 3)
    assert f"<h1>Homework notes ({total})</h1>" in listed
    assert "1 homework note cannot be read right now." in listed


def test_a_row_not_held_as_the_store_writes_it_is_said_to_be_unreadable_on_every_page() -> None:
    with browser() as client:
        store = state_of(client).project_state
        name = save_note(client, "Words that will be padded")
        store._connection.execute("UPDATE homework_captures SET text = '  PADDED WORDS  '")
        store._connection.commit()
        pages = [client.get(where) for where in (HER_PAGE, NOTES_PAGE, FAMILY, note_href(name))]

    for page in pages:
        assert page.status_code == 200
        assert "cannot be read right now" in page.text
        assert "PADDED WORDS" not in page.text


@pytest.mark.parametrize(
    "statement",
    [
        "UPDATE homework_captures SET text = ?",
        "UPDATE homework_captures SET course = ?",
        "UPDATE homework_captures SET title = ?",
    ],
)
def test_bytes_in_a_text_column_are_never_shown_as_her_words(statement: str) -> None:
    with browser() as client:
        store = state_of(client).project_state
        name = save_note(client, "Ordinary words", course="Geometry")
        store._connection.execute(statement, (b"INJECTED <b>bytes</b>",))
        store._connection.commit()
        pages = [client.get(where) for where in (HER_PAGE, NOTES_PAGE, FAMILY, note_href(name))]

    for page in pages:
        assert page.status_code == 200
        assert "INJECTED" not in page.text
        assert "Ordinary words" not in page.text
        assert "cannot be read right now" in page.text


@pytest.mark.parametrize("held", [b"INJECTED reference", "INJECTED words", "CAPITALS"])
def test_a_reference_that_is_no_id_shows_the_request_and_reflects_nothing_it_holds(
    held: object,
) -> None:
    """The help store keeps only the note's id, written one way. Bytes, other words, or an id
    in capitals in its place name no note: the request is still shown, the note is said to be
    unavailable, and what the column holds reaches no page and no JSON answer."""
    with browser() as client:
        state = state_of(client)
        name = save_note(client, "Ordinary words")
        assert ask_about(client, name, "which part?").status_code == 303
        value = name.upper() if held == "CAPITALS" else held
        state.help_requests._connection.execute("UPDATE help_requests SET capture_id = ?", (value,))
        state.help_requests._connection.commit()
        asked = state.help_requests.open_requests()[0]
        answers = [
            client.get(HER_PAGE),
            client.get(FAMILY),
            client.get("/student/help-requests"),
            client.get("/parent/help-requests"),
            client.post(f"/parent/help-requests/{asked.request_id}/accept", json={}),
            client.post(f"/parent/help-requests/{asked.request_id}/resolve", json={}),
        ]

    unavailable = {"capture_id": None, "text": None, "archived": False, "unavailable": True}
    for answer in answers:
        assert answer.status_code == 200
        assert "which part?" in answer.text
        assert "INJECTED" not in answer.text
        assert name.upper() not in answer.text
    for page in answers[:2]:
        assert "homework note cannot be read right now." in page.text
        assert "Open homework note" not in page.text.split("which part?", 1)[1].split("</", 3)[0]
    for listing in answers[2:4]:
        assert listing.json()[0]["about_note"] == unavailable
    for moved in answers[4:]:
        assert moved.json()["about_note"] == unavailable


BROKEN_LINES = {
    "a change that is no json": "UPDATE capture_events SET after = '{' WHERE revision = 1",
    "a change of no kind": "UPDATE capture_events SET operation = 'guess' WHERE revision = 2",
    "a revision skipped": "UPDATE capture_events SET revision = 12 WHERE revision = 2",
    "no first save": "DELETE FROM capture_events WHERE revision = 1",
    "a before that never stood": (
        "UPDATE capture_events SET before = json_set(before, '$.text', 'Never stood') "
        "WHERE revision = 2"
    ),
}


@pytest.mark.parametrize("damage", sorted(BROKEN_LINES))
def test_a_note_its_own_page_cannot_show_is_not_shown_or_asked_about_through_help(
    damage: str,
) -> None:
    """The help page and a request for help read a note as its own page does, its line of
    changes included. A line that is damaged or broken makes the note unavailable there too:
    no words, no form, and a request that is refused with her question kept and nothing sent."""
    with browser() as client:
        state = state_of(client)
        store = state.project_state
        name = save_note(client, "WORDS OF THE NOTE")
        change_note(client, name, "edit", text="WORDS OF THE NOTE, EDITED", course="", due_date="")
        action = note_action(name, "ask-for-help")
        opened_before = whole_form(client.get(note_help_href(name)).text, action)
        store._connection.execute(BROKEN_LINES[damage])
        store._connection.commit()

        own_page = client.get(note_href(name))
        opened = client.get(note_help_href(name))
        asked = client.post(
            action, data={**opened_before, "note": "QUESTION <b>kept</b>"}, headers=PAGE_HEADERS
        )
        sent = state.help_requests.open_requests()

    assert escape(NOTE_CANNOT_BE_READ) in own_page.text
    assert opened.status_code == 200
    assert escape(NOTE_CANNOT_BE_READ) in opened.text
    for page in (own_page, opened):
        assert escape(NOTE_UNREADABLE) not in page.text
    assert escape(NOTE_GONE) not in opened.text
    assert f'action="{action}"' not in opened.text
    assert asked.status_code == 500
    assert asked.text.count(" autofocus") == 1
    assert escape(NOTE_UNREADABLE) in asked.text
    assert "QUESTION &lt;b&gt;kept&lt;/b&gt;" in asked.text
    for answer in (opened, asked):
        assert "WORDS OF THE NOTE" not in answer.text
    assert sent == []


def test_the_help_page_reads_a_sound_note_with_its_changes_in_one_snapshot() -> None:
    with browser() as client:
        state = state_of(client)
        name = save_note(client, "WORDS OF THE NOTE")
        change_note(client, name, "edit", text="WORDS OF THE NOTE, EDITED", course="", due_date="")
        seen: list[str] = []
        state.project_state._connection.set_trace_callback(seen.append)
        opened = client.get(note_help_href(name))
        state.project_state._connection.set_trace_callback(None)
        asked = ask_about(client, name, "")
        sent = state.help_requests.open_requests()

    reads = [line for line in seen if line.lstrip().upper().startswith("SELECT")]
    assert opened.status_code == 200
    assert "WORDS OF THE NOTE, EDITED" in opened.text
    assert len(reads) == 2
    assert seen[0].upper().startswith("BEGIN")
    assert seen[-1].upper() == "COMMIT"
    assert asked.status_code == 303
    assert [item.capture_id for item in sent] == [name]


@pytest.mark.parametrize("with_its_first_change", [False, True])
def test_a_note_given_a_place_ahead_of_the_first_is_shown_nowhere_and_reorders_nothing(
    with_its_first_change: bool,
) -> None:
    with browser() as client:
        store = state_of(client).project_state
        save_note(client, "FIRST NOTE")
        damaged = save_note(client, "SECOND NOTE")
        save_note(client, "THIRD NOTE")
        store._connection.execute(
            "UPDATE homework_captures SET created_order = 0 WHERE capture_id = ?", (damaged,)
        )
        if with_its_first_change:
            store._connection.execute(
                "UPDATE capture_events SET sequence = 0 WHERE capture_id = ?", (damaged,)
            )
        store._connection.commit()
        pages = [client.get(where).text for where in (HER_PAGE, NOTES_PAGE, FAMILY)]
        own = client.get(note_href(damaged)).text

    for page in pages:
        shown = list(dict.fromkeys(re.findall(r"FIRST NOTE|SECOND NOTE|THIRD NOTE", page)))
        assert shown == ["FIRST NOTE", "THIRD NOTE"]
        assert "SECOND NOTE" not in page
        assert "1 homework note cannot be read right now." in page
    assert "SECOND NOTE" not in own
    assert "cannot be read right now" in own


# --------------------------------------------------------- what a note never does


def test_no_word_of_a_note_reaches_a_planner_or_a_critic_and_no_plan_goes_stale() -> None:
    """Saved before the plan is asked for, with a first brief and a revision after the
    critic's dissent, and changed again while the plan waits."""
    planners: list[Scripted] = []  # type: ignore[type-arg]
    critics: list[Scripted] = []  # type: ignore[type-arg]
    with browser(key=True) as client:
        client.app.dependency_overrides[plan_graphs] = scripted_graphs(  # type: ignore[attr-defined]
            lambda: [fixture_week_plan(), fixture_week_plan()],
            lambda: [dissent(), accepting()],
            planners=planners,
            critics=critics,
        )
        store = state_of(client).project_state
        before = [item.model_dump() for item in store.all_assignments()]
        name = save_note(client, "ZEBRA-WORDS about the canal essay", course="ZEBRA-CLASS")
        ask_about(client, name, "ZEBRA-QUESTION")

        assert client.post("/student/actions/plan").status_code == 303
        waiting = state_of(client).drafts.latest_for(PLAN_DATE)
        change_note(client, name, "edit", text="ZEBRA-LATER", course="", due_date="")
        change_note(client, name, "archive")
        week = client.get(HER_PAGE, params={"week": FIXTURE_WEEK}).text
        family = client.get(FAMILY).text
        after = [item.model_dump() for item in store.all_assignments()]

    assert waiting is not None
    assert [len(planner.briefs) for planner in planners] == [2]
    assert [len(critic.briefs) for critic in critics] == [2]
    for asked in (*planners, *critics):
        sent = " ".join(str(message.content) for brief in asked.briefs for message in brief)
        assert "ZEBRA" not in sent
        assert "homework note" not in sent.lower()
    assert ASSIGNMENTS_CHANGED not in week
    assert THEIR_ASSIGNMENTS_CHANGED not in family
    assert after == before


def test_her_page_says_who_reads_a_note_and_that_no_model_does() -> None:
    with browser() as client:
        week = client.get(HER_PAGE).text

    shared = week[week.index('id="what-is-shared"') :]
    assert "homework notes" in shared.lower()
    assert "never sent" in shared.lower()


# ---------------------------------------------------- a write and then a read fail


@pytest.mark.parametrize("step", ["edit", "archive"])
def test_a_change_the_file_refuses_is_said_even_when_the_note_cannot_be_read_back(
    step: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    with browser() as client:
        store = state_of(client).project_state
        name = save_note(client)
        page = client.get(note_href(name, edit="1") if step == "edit" else note_href(name)).text
        action = note_action(name, step)
        typed = (
            {"text": "<i>my</i> new words", "course": "", "due_date": ""} if step == "edit" else {}
        )
        fields = {**form_fields(page, action), **typed}
        before = store._connection.execute("SELECT * FROM capture_events").fetchall()
        tried: list[str] = []

        def refuses(*args: object, **kwargs: object) -> None:
            tried.append("write")
            msg = "the file refused"
            raise sqlite3.OperationalError(msg)

        monkeypatch.setattr(store, "_append_capture_event_locked", refuses)
        readable = client.post(action, data=fields, headers=PAGE_HEADERS)
        seen: list[str] = []
        began: list[int] = []

        def unread(*args: object, **kwargs: object) -> None:
            began.append(len(seen))
            msg = "the file cannot be read"
            raise sqlite3.OperationalError(msg)

        monkeypatch.setattr(store, "sound_capture_history", unread)
        store._connection.set_trace_callback(seen.append)
        plain = client.post(action, data=fields, headers=PAGE_HEADERS)
        store._connection.set_trace_callback(None)
        monkeypatch.undo()
        after = store._connection.execute("SELECT * FROM capture_events").fetchall()

    for answer in (readable, plain):
        assert answer.status_code == 500
        assert answer.text.count(" autofocus") == 1
        assert "Your changes are saved" not in answer.text
        assert "<i>my</i>" not in answer.text
        if step == "edit":
            assert str(escape(NOTE_NOT_SAVED)) in answer.text
            assert "&lt;i&gt;my&lt;/i&gt; new words" in answer.text
    assert "<h1>Update not saved</h1>" in plain.text
    assert f'href="{NOTES_PAGE}"' in plain.text
    assert tried == ["write", "write"]
    assert len(began) == 1
    assert [
        line for line in seen[began[0] :] if line.strip().upper() not in ("ROLLBACK", "COMMIT")
    ] == []
    assert after == before


def test_a_request_the_file_refuses_is_said_with_her_question_kept_and_nothing_read_again(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The page that says so is made from the note read before the write, so it reads no
    store after the refusal, keeps her question as she typed it, and offers the button again."""
    with browser() as client:
        state = state_of(client)
        name = save_note(client)
        action = note_action(name, "ask-for-help")
        opened = client.get(note_help_href(name)).text
        fields = {**form_fields(opened, action), "note": "<b>which</b> part first?"}
        seen: list[str] = []
        refused_at: list[int] = []

        def refuses(*args: object, **kwargs: object) -> None:
            refused_at.append(len(seen))
            msg = "the file refused"
            raise sqlite3.OperationalError(msg)

        monkeypatch.setattr(state.help_requests, "ask_once", refuses)
        state.project_state._connection.set_trace_callback(seen.append)
        answer = client.post(action, data=fields, headers=PAGE_HEADERS)
        state.project_state._connection.set_trace_callback(None)
        monkeypatch.undo()
        sent = state.help_requests.open_requests()
        again = client.post(action, data=whole_form(answer.text, action), headers=PAGE_HEADERS)
        asked = state.help_requests.open_requests()

    assert answer.status_code == 500
    assert answer.text.count(" autofocus") == 1
    assert 'id="note-problem"' in answer.text
    assert str(escape(HELP_NOT_ASKED)) in answer.text
    assert "&lt;b&gt;which&lt;/b&gt; part first?" in answer.text
    assert "<b>which</b>" not in answer.text
    assert WORDS in answer.text
    assert str(escape(NOTE_ASKED)) not in answer.text
    assert len(refused_at) == 1
    assert [
        line for line in seen[refused_at[0] :] if line.strip().upper() not in ("ROLLBACK", "COMMIT")
    ] == []
    assert sent == []
    assert again.status_code == 303
    assert [(item.capture_id, item.note) for item in asked] == [(name, "<b>which</b> part first?")]


# ------------------------------------------------------------ one request per form, about a note

ALREADY_SENT = "That request was already sent."
FORM_USED = "This form was already used. Open a new help form to ask again."
FORM_SENT_OTHER_WORDS = "This form already sent a request"


def help_form(client: TestClient, name: str) -> dict[str, str]:
    """What the note's Ask for help form sends, a fresh id among them."""
    return whole_form(client.get(note_help_href(name)).text, note_action(name, "ask-for-help"))


def help_held(client: TestClient) -> tuple[list[object], int]:
    """Every request the store holds as it stands, and how many form ids it keeps."""
    held = state_of(client).help_requests
    ids = held._connection.execute("SELECT count(*) FROM help_request_ids").fetchone()[0]
    return [*held.open_requests(), *held.recently_resolved()], ids


def help_row(page: str, request_id: str) -> str:
    start = page.index(f'<li class="help-request" id="help-{request_id}" tabindex="-1">')
    return page[start : page.index("</li>", start)]


@pytest.mark.parametrize("next_day", [False, True], ids=["same day", "after midnight"])
@pytest.mark.parametrize(
    ("steps", "reply", "stands"),
    [
        ([], None, "Waiting for a parent to respond."),
        (["accept"], None, "A parent is on it."),
        (["accept"], "Review the graph together after dinner.", "A parent is on it."),
        (["accept", "resolve"], None, "A parent closed this request on "),
        (["accept", "resolve"], "Page 12 has the steps.", "A parent closed this request on "),
    ],
    ids=["requested", "taken up", "taken up with a reply", "resolved", "resolved with a reply"],
)
def test_the_same_note_form_sent_again_lands_on_the_request_as_it_stands(
    steps: list[str],
    reply: str | None,
    stands: str,
    next_day: bool,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    with browser() as client:
        name = save_note(client)
        action = note_action(name, "ask-for-help")
        form = {**help_form(client, name), "note": "Which part comes first?"}
        first = client.post(action, data=form, headers=PAGE_HEADERS)
        asked = state_of(client).help_requests.open_requests()[0]
        for step in steps:
            said = {"response": reply} if reply and step == steps[-1] else {}
            moved = client.post(f"/parent/help-requests/{asked.request_id}/{step}", json=said)
            assert moved.status_code == 200, moved.text
        before = help_held(client)
        if next_day:
            clock = state_of(client).clock
            monkeypatch.setattr(clock, "_instant", clock.now() + timedelta(days=1))
        again = client.post(action, data=form, headers=PAGE_HEADERS)
        landed = client.get(again.headers["location"])
        after = help_held(client)

    assert first.status_code == 303
    assert again.status_code == 303
    assert again.headers["location"] == f"{HER_PAGE}?asked_again={asked.request_id}#help-result"
    assert landed.status_code == 200
    assert landed.text.count(ALREADY_SENT) == 1
    row = help_row(landed.text, asked.request_id)
    assert f'id="help-result" tabindex="-1">{ALREADY_SENT}' in row
    assert "Which part comes first?" in row
    assert stands in row
    assert (f"They said: <q>{escape(reply)}</q>" in row) is (reply is not None)
    assert f'href="{note_href(name)}"' in row
    assert 'id="help-older"' not in landed.text
    assert after == before


@pytest.mark.parametrize("case", ["other words", "taken back"])
def test_a_note_form_that_already_asked_keeps_her_question_and_asks_nothing_more(
    case: str,
) -> None:
    with browser() as client:
        name = save_note(client)
        action = note_action(name, "ask-for-help")
        form = help_form(client, name)
        client.post(action, data={**form, "note": "which part?"}, headers=PAGE_HEADERS)
        request_id = state_of(client).help_requests.open_requests()[0].request_id
        if case == "taken back":
            client.post(f"/student/actions/take-back-help/{request_id}")
        words = "the second part" if case == "other words" else "which part?"
        refused = client.post(action, data={**form, "note": words}, headers=PAGE_HEADERS)
        again = whole_form(refused.text, action)
        sent = state_of(client).help_requests.open_requests()

    assert refused.status_code == 409
    assert (FORM_SENT_OTHER_WORDS if case == "other words" else FORM_USED) in refused.text
    assert again["note"] == words
    assert again["request_id"] != form["request_id"]
    assert len(sent) == (1 if case == "other words" else 0)


@pytest.mark.parametrize(
    "shape", ["no id", "id twice", "question twice", "bad id", "unknown field"]
)
def test_a_note_form_that_is_not_whole_asks_nothing_and_keeps_the_first_question(
    shape: str,
) -> None:
    with browser() as client:
        name = save_note(client)
        action = note_action(name, "ask-for-help")
        form_id = help_form(client, name)["request_id"]
        first = ("note", "First synthetic question")
        fields = {
            "no id": [first],
            "id twice": [first, ("request_id", form_id), ("request_id", form_id)],
            "question twice": [
                first,
                ("note", "Second synthetic question"),
                ("request_id", form_id),
            ],
            "bad id": [first, ("request_id", "not-an-id")],
            "unknown field": [first, ("request_id", form_id), ("status", "done")],
        }[shape]
        refused = client.post(
            action,
            content="&".join(f"{key}={value.replace(' ', '+')}" for key, value in fields),
            headers={**PAGE_HEADERS, "Content-Type": "application/x-www-form-urlencoded"},
        )
        again = whole_form(refused.text, action)
        sent = state_of(client).help_requests.open_requests()

    assert refused.status_code == 422
    assert str(escape(HELP_FORM_NOT_WHOLE)) in refused.text
    assert again["note"] == "First synthetic question"
    assert "Second synthetic question" not in refused.text
    assert again["request_id"] not in ("", "not-an-id", form_id)
    assert sent == []


def test_a_note_question_sent_as_a_file_asks_nothing_and_says_why() -> None:
    with browser() as client:
        name = save_note(client)
        action = note_action(name, "ask-for-help")
        form_id = help_form(client, name)["request_id"]
        refused = client.post(
            action,
            data={"request_id": form_id},
            files={"note": ("question.txt", b"file words", "text/plain")},
            headers=PAGE_HEADERS,
        )
        held = help_held(client)

    assert refused.status_code == 422
    assert str(escape(HELP_FORM_NOT_WHOLE)) in refused.text
    assert "file words" not in refused.text
    assert held == ([], 0)


@pytest.mark.parametrize("sent", ["the same words", "other words", "taken back"])
@pytest.mark.parametrize("what", ["unreadable", "gone", "read_failure"])
def test_a_note_form_sent_again_is_answered_by_its_id_whatever_became_of_the_note(
    what: str, sent: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    with browser() as client:
        state = state_of(client)
        store = state.project_state
        name = save_note(client)
        action = note_action(name, "ask-for-help")
        form = {**help_form(client, name), "note": "Which part comes first?"}
        assert client.post(action, data=form, headers=PAGE_HEADERS).status_code == 303
        asked = state.help_requests.open_requests()[0]
        if sent == "taken back":
            client.post(f"/student/actions/take-back-help/{asked.request_id}")
        before = help_held(client)
        if what == "read_failure":

            def unread(*args: object, **kwargs: object) -> None:
                msg = "the file cannot be read"
                raise sqlite3.OperationalError(msg)

            monkeypatch.setattr(store, "sound_capture_history", unread)
        else:
            store._connection.execute(
                {
                    "unreadable": "UPDATE homework_captures SET attribution = 'no json' "
                    "WHERE capture_id = ?",
                    "gone": "DELETE FROM homework_captures WHERE capture_id = ?",
                }[what],
                (name,),
            )
            store._connection.commit()
        words = "Where do I start?" if sent == "other words" else "Which part comes first?"
        again = client.post(action, data={**form, "note": words}, headers=PAGE_HEADERS)
        monkeypatch.undo()
        landed = client.get(again.headers.get("location", HER_PAGE))
        after = help_held(client)

    assert after == before
    if sent == "the same words":
        assert again.status_code == 303
        assert again.headers["location"] == f"{HER_PAGE}?asked_again={asked.request_id}#help-result"
        assert landed.status_code == 200
        row = help_row(landed.text, asked.request_id)
        assert f'id="help-result" tabindex="-1">{ALREADY_SENT}' in row
        assert "Which part comes first?" in row
    else:
        fresh = f"{HER_PAGE}#ask-for-help"
        assert again.status_code == 409
        assert (FORM_USED if sent == "taken back" else FORM_SENT_OTHER_WORDS) in again.text
        assert f"readonly>{words}</textarea>" in again.text
        assert f'<a href="{fresh}">Open a new help form</a>' in again.text
        assert (f'href="{note_href(name)}"' in again.text) is (what != "gone")
        assert whole_form(landed.text, "/student/actions/ask-for-help")["request_id"]


@pytest.mark.parametrize("signed", [False, True], ids=["sign-in off", "signed in"])
@pytest.mark.parametrize("what", ["unreadable", "read_failure", "not found"])
@pytest.mark.parametrize("sent", ["other words", "taken back"])
def test_a_note_form_that_already_asked_says_so_through_failed_reads_and_after_they_end(
    sent: str, what: str, signed: bool, tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    app = create_app(signed_in_household(tmp_path)) if signed else browser().app
    with TestClient(app, follow_redirects=False, headers=SAME_ORIGIN) as client:
        if signed:
            client.post("/sign-in", data={"passphrase": HERS})
        state = state_of(client)
        store = state.project_state
        name = save_note(client)
        action = note_action(name, "ask-for-help")
        form = {**help_form(client, name), "note": "Synthetic first question"}
        assert client.post(action, data=form, headers=PAGE_HEADERS).status_code == 303
        asked = state.help_requests.open_requests()[0]
        if sent == "taken back":
            client.post(f"/student/actions/take-back-help/{asked.request_id}")
        words = "Changed synthetic question" if sent == "other words" else form["note"]
        before = help_held(client)
        kept = store._connection.execute(
            "SELECT attribution FROM homework_captures WHERE capture_id = ?", (name,)
        ).fetchone()[0]
        if what == "unreadable":
            store._connection.execute(
                "UPDATE homework_captures SET attribution = 'no json' WHERE capture_id = ?",
                (name,),
            )
            store._connection.commit()
        else:

            def unread(*args: object, **kwargs: object) -> None:
                if what == "read_failure":
                    msg = "the file cannot be read"
                    raise sqlite3.OperationalError(msg)

            monkeypatch.setattr(store, "sound_capture_history", unread)
        failed = [
            client.post(action, data={**form, "note": words}, headers=PAGE_HEADERS)
            for _ in range(2)
        ]
        monkeypatch.undo()
        store._connection.execute(
            "UPDATE homework_captures SET attribution = ? WHERE capture_id = ?", (kept, name)
        )
        store._connection.commit()
        recovered = client.post(action, data={**form, "note": words}, headers=PAGE_HEADERS)
        held = help_held(client)
        fresh = whole_form(client.get(note_help_href(name)).text, action)
        sent_fresh = client.post(action, data={**fresh, "note": words}, headers=PAGE_HEADERS)
        after = help_held(client)
        now = state.help_requests.open_requests()

    problem = FORM_USED if sent == "taken back" else FORM_SENT_OTHER_WORDS
    link = f"{HER_PAGE}#ask-for-help"
    for answer in failed:
        assert answer.status_code == 409
        assert problem in answer.text
        assert f"readonly>{words}</textarea>" in answer.text
        assert f'<a href="{link}">Open a new help form</a>' in answer.text
    assert recovered.status_code == 409
    assert problem in recovered.text
    again = whole_form(recovered.text, action)
    assert again["note"] == words
    assert again["request_id"] not in (form["request_id"], fresh["request_id"])
    assert held == before
    assert sent_fresh.status_code == 303
    assert after[1] == before[1] + 1
    assert len(after[0]) == len(before[0]) + 1
    assert [item.note for item in now if item.request_id == fresh["request_id"]] == [words]


def test_a_note_form_whose_id_cannot_be_looked_up_asks_nothing_and_keeps_the_question(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    with browser() as client:
        state = state_of(client)
        name = save_note(client)
        action = note_action(name, "ask-for-help")
        form = {**help_form(client, name), "note": "Which part comes first?"}

        def unread(*args: object, **kwargs: object) -> None:
            msg = "the file cannot be read"
            raise sqlite3.OperationalError(msg)

        monkeypatch.setattr(state.help_requests, "already_asked", unread)
        answer = client.post(action, data=form, headers=PAGE_HEADERS)
        monkeypatch.undo()
        held = help_held(client)

    assert answer.status_code == 500
    assert str(escape(HELP_NOT_ASKED)) in answer.text
    assert "Which part comes first?" in answer.text
    assert held == ([], 0)


def test_the_later_of_two_crossed_sends_of_a_note_form_lands_on_the_request(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    with browser() as client:
        state = state_of(client)
        name = save_note(client)
        action = note_action(name, "ask-for-help")
        form = {**help_form(client, name), "note": "Which part comes first?"}
        assert client.post(action, data=form, headers=PAGE_HEADERS).status_code == 303
        asked = state.help_requests.open_requests()[0]
        before = help_held(client)
        monkeypatch.setattr(state.help_requests, "already_asked", lambda *_, **__: None)
        again = client.post(action, data=form, headers=PAGE_HEADERS)
        monkeypatch.undo()
        landed = client.get(again.headers["location"])
        after = help_held(client)

    assert again.status_code == 303
    assert again.headers["location"] == f"{HER_PAGE}?asked_again={asked.request_id}#help-result"
    row = help_row(landed.text, asked.request_id)
    assert f'id="help-result" tabindex="-1">{ALREADY_SENT}' in row
    assert after == before
