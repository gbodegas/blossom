"""Her homework notes through the pages: a first save that asks for words only, every
refusal with her words kept, edits and the archive by revision, what a parent may read and
may not do, and results that stay true when another device got there first.

The fixture week through the app, a pinned clock, and forms read from the page's own HTML.
No model is asked anything here, and no note becomes an assignment.
"""

import html
import pathlib
import re
import sqlite3
from datetime import date
from urllib.parse import urlencode

import pytest
from fastapi.testclient import TestClient
from markupsafe import escape

from blossom.app import create_app
from blossom.captures import (
    CAPTURE_TEXT_MAX_LENGTH,
    HOUSEHOLD,
    STUDENT,
    capture_id_from,
    new_capture_id,
)
from blossom.routes.captures import (
    NOTE_ALREADY_SAVED,
    NOTE_ARCHIVED,
    NOTE_CHANGED,
    NOTE_DATE_UNREADABLE,
    NOTE_EDITED,
    NOTE_GONE,
    NOTE_ID_TAKEN,
    NOTE_NEEDS_A_DATE_CHOICE,
    NOTE_NEEDS_WORDS,
    NOTE_NOT_SAVED,
    NOTE_RESTORED,
    NOTE_SAVED,
    NOTE_SAVED_EARLIER,
    NOTE_TOO_LONG,
    NOTE_UNREADABLE,
    revision_of,
)
from blossom.routes.navigation import (
    ARCHIVED_NOTES_PAGE,
    NEW_NOTE_PAGE,
    NOTE_ACTIONS,
    NOTES_PAGE,
    note_action,
    note_help_href,
    note_href,
)
from blossom.routes.student import BAD_FORM, NOT_HERS_TO_UPDATE
from blossom.stores.project_state import ProjectStateStore
from tests.support import (
    HER_PAGE,
    HERS,
    PAGE_HEADERS,
    SAME_ORIGIN,
    THEIRS,
    Answer,
    browser,
    form_fields,
    signed_in_household,
    state_of,
    whole_form,
)

WORDS = "Geometry questions 4-8, heard from a classmate"
NOT_YET = ("Add it to homework", "Link to homework", "Ready to add", "Add the class and title")


def new_form(client: TestClient) -> dict[str, str]:
    """The hidden fields of a fresh form: the id it was given, and nothing else of note."""
    page = client.get(NEW_NOTE_PAGE).text
    return form_fields(page, NOTE_ACTIONS)


def send(client: TestClient, fields: dict[str, str], **typed: str) -> Answer:
    return client.post(NOTE_ACTIONS, data={**fields, **typed}, headers=PAGE_HEADERS)


def saved_note(client: TestClient, text: str = WORDS, **typed: str) -> str:
    """Save one note through the page and return its id."""
    fields = new_form(client)
    answer = send(
        client,
        fields,
        text=text,
        course=typed.get("course", ""),
        due_date=typed.get("due_date", ""),
    )
    assert answer.status_code == 303, answer.text
    return fields["capture_id"]


def follow(client: TestClient, answer: Answer) -> str:
    assert answer.status_code == 303, answer.text
    return client.get(answer.headers["location"]).text


def tables(store: ProjectStateStore) -> tuple[list[tuple[object, ...]], list[tuple[object, ...]]]:
    notes = store._connection.execute("SELECT * FROM homework_captures ORDER BY capture_id")
    events = store._connection.execute("SELECT * FROM capture_events ORDER BY sequence")
    return notes.fetchall(), events.fetchall()


def said_first(page: str, sentence: str) -> bool:
    """A refusal said once, as the one thing on the page given the focus."""
    place = page.index('id="note-problem"')
    return (
        page.count(str(escape(sentence))) == 1
        and page.index(str(escape(sentence))) > place
        and page.count(" autofocus") == 1
        and "autofocus" in page[place : page.index(">", place)]
    )


# ------------------------------------------------------------ the first save


def test_the_form_asks_for_words_only_and_opening_it_writes_nothing() -> None:
    with browser() as client:
        store = state_of(client).project_state
        page = client.get(NEW_NOTE_PAGE)
        again = client.get(NEW_NOTE_PAGE)
        first, second = (
            form_fields(each.text, NOTE_ACTIONS)["capture_id"] for each in (page, again)
        )
        written = tables(store)

    assert page.status_code == 200
    assert "What do you need to remember?" in page.text
    assert "Write what you know. You can add the class and date later." in page.text
    assert ">Save homework note</button>" in page.text
    assert "<summary>Add details now</summary>" in page.text
    assert not re.search(r"<details[^>]* open", page.text)
    assert (
        " required"
        in page.text[page.text.index('name="text"') - 200 : page.text.index('name="text"') + 200]
    )
    assert capture_id_from(first) != capture_id_from(second)
    assert written == ([], [])
    assert not [words for words in NOT_YET if words in page.text]


def test_words_alone_are_saved_and_the_result_says_it_is_in_no_plan() -> None:
    with browser() as client:
        store = state_of(client).project_state
        before = [item.model_dump() for item in store.all_assignments()]
        fields = new_form(client)

        answer = send(client, fields, text=f"  {WORDS}  ", course="", due_date="")
        page = follow(client, answer)
        note = store.capture(fields["capture_id"])
        history = store.capture_history(fields["capture_id"])
        after = [item.model_dump() for item in store.all_assignments()]

    assert answer.headers["location"].endswith("#note-result")
    assert note is not None
    assert (note.text, note.course, note.due_date, note.revision) == (WORDS, None, None, 1)
    assert [event.authored_by for event in history] == [HOUSEHOLD]
    assert escape(NOTE_SAVED) in page
    assert NOTE_SAVED == "Homework note saved. It is not in a plan yet."
    assert 'id="note-result"' in page
    assert escape(WORDS) in page
    assert "Ask for help about this note" in page
    assert f'href="{NOTES_PAGE}"' in page
    assert not [words for words in NOT_YET if words in page]
    assert after == before


def test_the_same_form_again_is_already_saved_and_shows_the_note_as_it_stands_now() -> None:
    with browser() as client:
        store = state_of(client).project_state
        fields = new_form(client)
        typed = {"text": WORDS, "course": "Geometry", "due_date": "2026-08-21"}
        follow(client, send(client, fields, **typed))
        name = fields["capture_id"]

        again = follow(client, send(client, fields, **typed))
        note_page = client.get(note_href(name, edit="1")).text
        edit = note_action(name, "edit")
        follow(
            client,
            client.post(
                edit,
                data={
                    **form_fields(note_page, edit),
                    "text": "Questions 4-9",
                    "course": "Geometry",
                    "due_date": "",
                },
                headers=PAGE_HEADERS,
            ),
        )
        after_edit = follow(client, send(client, fields, **typed))
        archive = note_action(name, "archive")
        current = client.get(note_href(name)).text
        follow(
            client, client.post(archive, data=form_fields(current, archive), headers=PAGE_HEADERS)
        )
        after_archive = follow(client, send(client, fields, **typed))
        notes, events = tables(store)

    assert escape(NOTE_ALREADY_SAVED) in again
    assert escape(NOTE_SAVED) not in again
    assert escape(NOTE_ALREADY_SAVED) in after_edit
    assert "Questions 4-9" in after_edit
    assert escape(NOTE_ALREADY_SAVED) in after_archive
    assert "This note is archived." in after_archive
    assert "Questions 4-9" in after_archive
    assert len(notes) == 1
    assert len(events) == 3


def test_the_same_id_with_other_words_is_refused_and_both_are_shown() -> None:
    with browser() as client:
        store = state_of(client).project_state
        fields = new_form(client)
        follow(client, send(client, fields, text=WORDS, course="Geometry", due_date=""))
        before = tables(store)

        refused = send(client, fields, text="<b>Other</b> words", course="Geometry", due_date="")
        other_class = send(client, fields, text=WORDS, course="Algebra", due_date="")
        kept = form_fields(refused.text, NOTE_ACTIONS)
        after = tables(store)

    for answer in (refused, other_class):
        assert answer.status_code == 409
        assert said_first(answer.text, NOTE_ID_TAKEN)
        assert escape(WORDS) in answer.text
        assert escape(NOTE_SAVED) not in answer.text
    assert "&lt;b&gt;Other&lt;/b&gt; words" in refused.text
    assert "<b>Other</b>" not in refused.text
    assert 'value="Algebra"' in other_class.text
    assert kept["capture_id"] != fields["capture_id"]
    assert capture_id_from(kept["capture_id"])
    assert after == before


# ------------------------------------------------------------ an unreadable day


def test_a_day_that_cannot_be_read_keeps_everything_and_is_never_dropped_without_her_say() -> None:
    raw = '"><script>next tuesday</script>'
    with browser() as client:
        store = state_of(client).project_state
        fields = new_form(client)
        typed = {"text": WORDS, "course": "Geometry"}

        refused = send(client, fields, **typed, due_date=raw)
        round_trip = form_fields(refused.text, NOTE_ACTIONS)
        retried = send(client, round_trip, **typed, due_date="")
        nothing_yet = tables(store)
        corrected = send(client, round_trip, **typed, due_date="2026-08-21")
        with_a_day = store.capture(fields["capture_id"])

        second = new_form(client)
        send(client, second, **typed, due_date="soon")
        again = form_fields(send(client, second, **typed, due_date="soon").text, NOTE_ACTIONS)
        omitted = send(client, again, **typed, due_date="", choice="without_date")
        without = store.capture(second["capture_id"])

    assert refused.status_code == 422
    assert said_first(refused.text, NOTE_DATE_UNREADABLE)
    assert str(escape(raw)) in refused.text
    assert "<script>next tuesday" not in refused.text
    assert re.search(r'<input[^>]*type="date"[^>]*name="due_date"[^>]*value=""', refused.text)
    assert re.search(r"<details[^>]* open", refused.text)
    assert escape(WORDS) in refused.text
    assert 'value="Geometry"' in refused.text
    assert ">Save without the date</button>" in refused.text
    assert round_trip["capture_id"] == fields["capture_id"]
    assert round_trip["date_pending"] == "1"
    assert retried.status_code == 422
    assert said_first(retried.text, NOTE_NEEDS_A_DATE_CHOICE)
    assert nothing_yet == ([], [])
    assert corrected.status_code == 303
    assert with_a_day is not None
    assert with_a_day.due_date == date(2026, 8, 21)
    assert omitted.status_code == 303
    assert without is not None
    assert (without.due_date, without.course, without.text) == (None, "Geometry", WORDS)


def test_a_day_that_was_refused_is_said_on_every_later_answer_until_one_reads() -> None:
    """The form carries the words of the day it refused, so an answer about anything else
    still says them. A day that reads is kept when her words are what is refused, and takes
    the refused words off the page. Words the text rule would not keep are never shown."""
    raw = "next <b>tuesday</b>"
    long_words = "w" * (CAPTURE_TEXT_MAX_LENGTH + 1)
    typed = {"text": WORDS, "course": "Geometry"}
    with browser() as client:
        store = state_of(client).project_state
        fields = new_form(client)
        refused = send(client, fields, **typed, due_date=raw)
        carried = form_fields(refused.text, NOTE_ACTIONS)
        no_choice = send(client, carried, **typed, due_date="")
        carried_again = form_fields(no_choice.text, NOTE_ACTIONS)
        too_long = send(client, carried_again, text=long_words, course="Geometry", due_date="")
        forged = send(client, {**carried, "date_refused": "ring" + chr(7)}, **typed, due_date="")
        with_a_day = send(
            client, carried_again, text=long_words, course="Geometry", due_date="2026-08-21"
        )
        after_a_day = form_fields(with_a_day.text, NOTE_ACTIONS)
        written = tables(store)
        saved = send(client, after_a_day, **typed, due_date="2026-08-21")
        note = store.capture(fields["capture_id"])

    said = f'You wrote <q class="authored-text">{escape(raw)}</q>'
    assert (carried["date_pending"], carried["date_refused"]) == ("1", raw)
    assert carried_again == carried
    for answer, sentence in ((no_choice, NOTE_NEEDS_A_DATE_CHOICE), (too_long, NOTE_TOO_LONG)):
        assert answer.status_code == 422
        assert said_first(answer.text, sentence)
        assert said in answer.text
        assert "<b>tuesday" not in answer.text
        assert ">Save without the date</button>" in answer.text
        assert re.search(r"<details[^>]* open", answer.text)
    assert forged.status_code == 422
    assert said_first(forged.text, NOTE_NEEDS_A_DATE_CHOICE)
    assert "ring" not in forged.text
    assert with_a_day.status_code == 422
    assert said_first(with_a_day.text, NOTE_TOO_LONG)
    assert re.search(r'<input[^>]*name="due_date"[^>]*value="2026-08-21"', with_a_day.text)
    assert "You wrote" not in with_a_day.text
    assert ">Save without the date</button>" not in with_a_day.text
    assert set(after_a_day) == {"capture_id"}
    assert written == ([], [])
    assert saved.status_code == 303
    assert note is not None
    assert note.due_date == date(2026, 8, 21)


def test_an_edit_keeps_the_day_it_refused_the_same_way() -> None:
    with browser() as client:
        store = state_of(client).project_state
        name = saved_note(client)
        action = note_action(name, "edit")
        opened = form_fields(client.get(note_href(name, edit="1")).text, action)
        typed = {"text": WORDS, "course": ""}
        refused = client.post(
            action, data={**opened, **typed, "due_date": "friday"}, headers=PAGE_HEADERS
        )
        carried = form_fields(refused.text, action)
        no_choice = client.post(
            action, data={**carried, **typed, "due_date": ""}, headers=PAGE_HEADERS
        )
        note = store.capture(name)

    assert refused.status_code == 422
    assert (carried["date_pending"], carried["date_refused"]) == ("1", "friday")
    assert carried["revision"] == opened["revision"]
    assert no_choice.status_code == 422
    assert said_first(no_choice.text, NOTE_NEEDS_A_DATE_CHOICE)
    assert 'You wrote <q class="authored-text">friday</q>' in no_choice.text
    assert note is not None
    assert (note.revision, note.due_date) == (1, None)


# ------------------------------------- the form that comes back from any refusal

URLENCODED = {**PAGE_HEADERS, "Content-Type": "application/x-www-form-urlencoded"}


def opened_form(client: TestClient, surface: str) -> tuple[str, dict[str, str]]:
    """The action and the whole form of a fresh new note, or of an edit of a saved one."""
    if surface == "new":
        return NOTE_ACTIONS, whole_form(client.get(NEW_NOTE_PAGE).text, NOTE_ACTIONS)
    action = note_action(saved_note(client), "edit")
    name = action.split("/")[-2]
    return action, whole_form(client.get(note_href(name, edit="1")).text, action)


def broken(fields: dict[str, str], fault: str) -> str:
    """The form's body with one thing about it that these pages never send."""
    pairs = list(fields.items())
    if fault == "twice":
        pairs.append(("text", "a second text"))
    elif fault == "unknown":
        pairs.append(("role", "parent"))
    elif fault == "revision":
        pairs = [(name, "two" if name == "revision" else value) for name, value in pairs]
    return urlencode(pairs)


@pytest.mark.parametrize(
    ("surface", "fault"),
    [
        ("new", "twice"),
        ("new", "unknown"),
        ("edit", "twice"),
        ("edit", "unknown"),
        ("edit", "revision"),
    ],
)
def test_a_form_that_is_not_whole_comes_back_holding_the_day_it_carried(
    surface: str, fault: str
) -> None:
    """Nothing is written for it, and nothing of hers is lost to it: a day that reads is in
    the control, a day that does not is said back with its mark, and what comes next is the
    same as after any other refusal."""
    with browser() as client:
        store = state_of(client).project_state
        action, fields = opened_form(client, surface)
        fields.update(text="Keep these words", course="Math")
        before = tables(store)

        with_a_day = client.post(
            action, content=broken({**fields, "due_date": "2026-08-25"}, fault), headers=URLENCODED
        )
        kept = whole_form(with_a_day.text, action)
        unread = client.post(
            action, content=broken({**fields, "due_date": "friday"}, fault), headers=URLENCODED
        )
        marked = whole_form(unread.text, action)
        refused = tables(store)
        no_choice = client.post(action, data=marked, headers=PAGE_HEADERS)
        still_nothing = tables(store)
        saved = client.post(action, data=kept, headers=PAGE_HEADERS)
        name = kept.get("capture_id") or action.split("/")[-2]
        note = store.capture(name)

    for answer in (with_a_day, unread):
        assert answer.status_code == 422
        assert said_first(answer.text, BAD_FORM)
    assert (kept["text"], kept["course"], kept["due_date"]) == (
        "Keep these words",
        "Math",
        "2026-08-25",
    )
    assert "date_pending" not in kept
    assert (marked["due_date"], marked["date_pending"], marked["date_refused"]) == (
        "",
        "1",
        "friday",
    )
    assert 'You wrote <q class="authored-text">friday</q>' in unread.text
    assert refused == before
    assert no_choice.status_code == 422
    assert said_first(no_choice.text, NOTE_NEEDS_A_DATE_CHOICE)
    assert still_nothing == before
    assert saved.status_code == 303
    assert note is not None
    assert (note.text, note.course, note.due_date) == (
        "Keep these words",
        "Math",
        date(2026, 8, 25),
    )


@pytest.mark.parametrize("surface", ["new", "edit"])
@pytest.mark.parametrize("written", ["20260825", "2026-W35-2"])
def test_a_day_written_another_way_comes_back_as_the_date_control_writes_it(
    surface: str, written: str
) -> None:
    """A day Python reads and a native date control cannot hold is kept in the one spelling
    the control can, so another refusal cannot turn a day she gave into no day."""
    with browser() as client:
        store = state_of(client).project_state
        action, fields = opened_form(client, surface)
        long_words = "w" * (CAPTURE_TEXT_MAX_LENGTH + 1)
        refused = client.post(
            action,
            data={**fields, "text": long_words, "course": "Math", "due_date": written},
            headers=PAGE_HEADERS,
        )
        kept = whole_form(refused.text, action)
        saved = client.post(action, data={**kept, "text": "Short words"}, headers=PAGE_HEADERS)
        note = store.capture(kept.get("capture_id") or action.split("/")[-2])

    assert refused.status_code == 422
    assert said_first(refused.text, NOTE_TOO_LONG)
    assert kept["due_date"] == "2026-08-25"
    assert "date_pending" not in kept
    assert saved.status_code == 303
    assert note is not None
    assert note.due_date == date(2026, 8, 25)


@pytest.mark.parametrize("surface", ["new", "edit"])
@pytest.mark.parametrize(
    "controls",
    [
        {"choice": "other"},
        {"choice": " save"},
        {"date_pending": "no"},
        {"date_pending": " 1 "},
        {"date_refused": "friday"},
        {"date_pending": "0", "date_refused": "friday"},
    ],
)
def test_a_value_of_the_date_flow_these_pages_never_send_writes_nothing(
    surface: str, controls: dict[str, str]
) -> None:
    with browser() as client:
        store = state_of(client).project_state
        action, fields = opened_form(client, surface)
        before = tables(store)
        answer = client.post(
            action,
            data={**fields, "text": "Words <b>kept</b>", "course": "", "due_date": "", **controls},
            headers=PAGE_HEADERS,
        )
        kept = whole_form(answer.text, action)
        after = tables(store)

    assert answer.status_code == 422
    assert said_first(answer.text, BAD_FORM)
    assert kept["text"] == "Words <b>kept</b>"
    assert "<b>kept</b>" not in answer.text
    assert after == before


def test_a_refused_day_whose_mark_was_changed_writes_nothing_and_still_needs_her_choice() -> None:
    with browser() as client:
        store = state_of(client).project_state
        fields = new_form(client)
        typed = {"text": WORDS, "course": "Geometry"}
        refused = send(client, fields, **typed, due_date="friday")
        marked = whole_form(refused.text, NOTE_ACTIONS)
        changed = send(client, {**marked, "date_pending": "no"})
        returned = whole_form(changed.text, NOTE_ACTIONS)
        no_choice = send(client, returned)
        written = tables(store)
        omitted = send(client, returned, choice="without_date")

    assert changed.status_code == 422
    assert said_first(changed.text, BAD_FORM)
    assert (returned["date_pending"], returned["date_refused"]) == ("1", "friday")
    assert (returned["text"], returned["course"]) == (WORDS, "Geometry")
    assert ">Save without the date</button>" in changed.text
    assert no_choice.status_code == 422
    assert said_first(no_choice.text, NOTE_NEEDS_A_DATE_CHOICE)
    assert written == ([], [])
    assert omitted.status_code == 303


def test_saving_without_the_date_is_her_choice_for_the_save_it_was_pressed_for_only() -> None:
    """Pressed together with words that are refused, the choice saved nothing, so the form
    that comes back still carries the day and its mark, and a plain save still needs it."""
    with browser() as client:
        store = state_of(client).project_state
        fields = new_form(client)
        long_words = "w" * (CAPTURE_TEXT_MAX_LENGTH + 1)
        refused = send(client, fields, text=WORDS, course="", due_date="friday")
        marked = whole_form(refused.text, NOTE_ACTIONS)
        too_long = send(client, {**marked, "text": long_words}, choice="without_date")
        returned = whole_form(too_long.text, NOTE_ACTIONS)
        plain = send(client, {**returned, "text": WORDS}, choice="save")
        written = tables(store)
        omitted = send(client, {**returned, "text": WORDS}, choice="without_date")
        note = store.capture(fields["capture_id"])

    assert too_long.status_code == 422
    assert said_first(too_long.text, NOTE_TOO_LONG)
    assert (returned["date_pending"], returned["date_refused"]) == ("1", "friday")
    assert plain.status_code == 422
    assert said_first(plain.text, NOTE_NEEDS_A_DATE_CHOICE)
    assert written == ([], [])
    assert omitted.status_code == 303
    assert note is not None
    assert note.due_date is None


@pytest.mark.parametrize("surface", ["new", "edit"])
@pytest.mark.parametrize(
    ("typed", "target", "opens"),
    [
        ({"text": "w" * 501}, "note-text", False),
        ({"course": "c" * 61}, "note-course", True),
        ({"due_date": "friday"}, "note-due-date", True),
    ],
)
def test_a_refusal_about_a_field_links_to_it_and_the_field_points_back(
    surface: str, typed: dict[str, str], target: str, opens: bool
) -> None:
    with browser() as client:
        action, fields = opened_form(client, surface)
        answer = client.post(
            action,
            data={**fields, "text": WORDS, "course": "", "due_date": "", **typed},
            headers=PAGE_HEADERS,
        )
        general = client.post(
            action, data={**fields, "text": WORDS, "role": "parent"}, headers=PAGE_HEADERS
        )

    alert = re.search(r'<p class="problem"[^>]*id="note-problem"[^>]*>.*?</p>', answer.text, re.S)
    assert alert is not None
    assert answer.status_code == 422
    assert alert.group().count("<a ") == 1
    assert f'<a href="#{target}">' in alert.group()
    assert 'tabindex="-1"' in alert.group()
    assert answer.text.count(" autofocus") == 1
    field = re.search(rf'<(?:textarea|input)\b[^>]*id="{target}"[^>]*>', answer.text)
    assert field is not None
    assert 'aria-invalid="true"' in field.group()
    assert re.search(r'aria-describedby="[^"]*\bnote-problem\b', field.group())
    assert bool(re.search(r"<details[^>]* open", answer.text)) is opens
    whole = re.search(r'<p class="problem"[^>]*id="note-problem"[^>]*>.*?</p>', general.text, re.S)
    assert whole is not None
    assert "<a " not in whole.group()


# ------------------------------------------ a refusal over a note that is unavailable

TYPED = {"text": "UNSAVED <i>words</i> \U0001f600", "course": "UNSAVED <b>class</b>"}


def quiet_after_the_last_read(seen: list[str]) -> bool:
    """Whether the page that answered read no store to be made: nothing follows the last
    read of a note or its changes but where a transaction begins or ends."""
    reads = [
        place
        for place, line in enumerate(seen)
        if "homework_captures" in line or "capture_events" in line
    ]
    rest = seen[reads[-1] + 1 :] if reads else seen
    return all(line.split()[0].upper() in ("BEGIN", "ROLLBACK", "COMMIT") for line in rest)


@pytest.mark.parametrize("what", ["unreadable", "gone", "read_failure"])
@pytest.mark.parametrize("day", ["2026-08-25", "friday"])
def test_an_edit_refused_over_a_note_that_is_unavailable_keeps_everything_she_typed(
    what: str, day: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The form is opened first, and the note becomes unreadable, leaves the record, or the
    file cannot be read. The answer is the page that reads no store, with her words, the
    class, and the day or the words of a day that could not be read."""
    with browser() as client:
        store = state_of(client).project_state
        name = saved_note(client)
        action = note_action(name, "edit")
        opened = whole_form(client.get(note_href(name, edit="1")).text, action)
        if what == "unreadable":
            store._connection.execute(
                "UPDATE homework_captures SET attribution = 'no json' WHERE capture_id = ?", (name,)
            )
        elif what == "gone":
            store._connection.execute("DELETE FROM homework_captures WHERE capture_id = ?", (name,))
        store._connection.commit()
        before = tables(store)
        if what == "read_failure":

            def unread(*args: object, **kwargs: object) -> None:
                msg = "the file cannot be read"
                raise sqlite3.OperationalError(msg)

            monkeypatch.setattr(store, "capture", unread)
            monkeypatch.setattr(store, "_capture_locked", unread)
        seen: list[str] = []
        store._connection.set_trace_callback(seen.append)
        answer = client.post(
            action, data={**opened, **TYPED, "due_date": day}, headers=PAGE_HEADERS
        )
        store._connection.set_trace_callback(None)
        monkeypatch.undo()
        after = tables(store)
        left_open = store._connection.in_transaction
        still_opens = client.get(note_href(name))

    refused_first = day == "friday"
    expected = 422 if refused_first else {"unreadable": 500, "gone": 404, "read_failure": 500}[what]
    assert answer.status_code == expected
    assert "<h1>Update not saved</h1>" in answer.text
    assert answer.text.count(" autofocus") == 1
    assert "UNSAVED &lt;i&gt;words&lt;/i&gt; \U0001f600" in answer.text
    assert "UNSAVED &lt;b&gt;class&lt;/b&gt;" in answer.text
    assert "<i>words</i>" not in answer.text
    assert day in answer.text
    assert escape(NOTE_EDITED) not in answer.text
    assert "<form" not in answer.text.split("<main", 1)[-1]
    assert f'href="{NOTES_PAGE}"' in answer.text
    assert quiet_after_the_last_read(seen)
    assert after == before
    assert not left_open
    assert still_opens.status_code == (404 if what == "gone" else 200)


@pytest.mark.parametrize("day", ["2026-09-23", "friday"])
def test_an_edit_behind_an_archive_keeps_every_field_and_leads_to_the_note(day: str) -> None:
    """Two devices: the edit is open on one, the note is archived on the other, and the edit
    is then sent with other words, another class, and a day. Nothing is saved, nothing is
    brought back, all of it is there to copy, and the way to the note is one link."""
    with browser() as client:
        store = state_of(client).project_state
        name = saved_note(client)
        action = note_action(name, "edit")
        opened = whole_form(client.get(note_href(name, edit="1")).text, action)
        archive = note_action(name, "archive")
        page = client.get(note_href(name)).text
        client.post(archive, data=form_fields(page, archive), headers=PAGE_HEADERS)
        before = tables(store)
        answer = client.post(
            action, data={**opened, **TYPED, "due_date": day}, headers=PAGE_HEADERS
        )
        after = tables(store)
        note = store.capture(name)

    assert answer.status_code == (422 if day == "friday" else 409)
    assert answer.text.count(" autofocus") == 1
    assert "UNSAVED &lt;i&gt;words&lt;/i&gt; \U0001f600" in answer.text
    assert "UNSAVED &lt;b&gt;class&lt;/b&gt;" in answer.text
    assert day in answer.text
    assert "This note is archived." in answer.text
    assert "not saved" in answer.text
    assert f'<a href="{note_href(name)}">' in answer.text
    assert f'action="{action}"' not in answer.text
    assert 'href="#note-' not in answer.text
    assert after == before
    assert note is not None
    assert (note.archived, note.text) == (True, WORDS)


@pytest.mark.parametrize(
    "damage",
    [
        "UPDATE capture_events SET after = '{' WHERE revision = 1",
        "UPDATE capture_events SET revision = 12 WHERE revision = 2",
        "DELETE FROM capture_events WHERE revision = 1",
    ],
)
@pytest.mark.parametrize("step", ["edit", "archive", "the same form again"])
def test_a_note_whose_changes_are_not_one_line_takes_no_change_and_keeps_her_words(
    damage: str, step: str
) -> None:
    """Through the pages: the change is refused with nothing written, the note's page says
    it cannot be read and gives no result, and what she typed into an edit is kept."""
    with browser() as client:
        store = state_of(client).project_state
        fields = new_form(client)
        first = {"text": WORDS, "course": "", "due_date": ""}
        send(client, fields, **first)
        name = fields["capture_id"]
        edit = note_action(name, "edit")
        opened = whole_form(client.get(note_href(name, edit="1")).text, edit)
        held = client.post(
            edit, data={**opened, "text": "Changed once"}, headers=PAGE_HEADERS
        ).headers["location"]
        current = whole_form(client.get(note_href(name, edit="1")).text, edit)
        store._connection.execute(damage)
        store._connection.commit()
        before = tables(store)

        answer: Answer
        if step == "edit":
            answer = client.post(edit, data={**current, **TYPED}, headers=PAGE_HEADERS)
        elif step == "archive":
            answer = client.post(
                note_action(name, "archive"), data={"revision": "2"}, headers=PAGE_HEADERS
            )
        else:
            answer = send(client, fields, **first)
        after = tables(store)
        page = client.get(held)

    assert answer.status_code == 500
    assert answer.text.count(" autofocus") == 1
    assert 'id="note-result"' not in answer.text
    if step == "edit":
        assert "UNSAVED &lt;i&gt;words&lt;/i&gt;" in answer.text
        assert "UNSAVED &lt;b&gt;class&lt;/b&gt;" in answer.text
    if step == "the same form again":
        assert escape(WORDS) in answer.text
    assert after == before
    assert page.status_code == 200
    assert escape(NOTE_UNREADABLE) in page.text
    assert 'id="note-result"' not in page.text
    assert "Changed once" not in page.text


def test_a_revision_is_read_only_as_these_pages_write_it() -> None:
    assert [revision_of({"revision": given}) for given in ("1", "12", "999999999")] == [
        1,
        12,
        999999999,
    ]
    for given in (
        "",
        "0",
        "00",
        "01",
        "1000000000",
        "\uff11",
        "\u0661",
        " 1",
        "1 ",
        "+1",
        "-1",
        "1.0",
    ):
        assert revision_of({"revision": given}) is None, given
    assert revision_of({}) is None


@pytest.mark.parametrize("given", ["0", "00", "01", "\uff11", "-1"])
@pytest.mark.parametrize("step", ["the same words", "archive again", "restore again"])
def test_a_revision_written_any_other_way_is_a_form_these_pages_did_not_make(
    given: str, step: str
) -> None:
    """Asking for what already stands is answered before revisions are compared, so a made-up
    revision must be refused where the form is read: 422, nothing written, no redirect that
    says already saved."""
    with browser() as client:
        store = state_of(client).project_state
        name = saved_note(client)
        if step == "the same words":
            action = note_action(name, "edit")
            fields = whole_form(client.get(note_href(name, edit="1")).text, action)
        else:
            action = note_action(name, "archive" if step == "archive again" else "restore")
            fields = {"revision": "1"}
            if step == "archive again":
                archive = note_action(name, "archive")
                page = client.get(note_href(name)).text
                client.post(archive, data=form_fields(page, archive), headers=PAGE_HEADERS)
        before = tables(store)
        answer = client.post(action, data={**fields, "revision": given}, headers=PAGE_HEADERS)
        after = tables(store)

    assert answer.status_code == 422
    assert said_first(answer.text, BAD_FORM)
    assert escape(NOTE_ALREADY_SAVED) not in answer.text
    if step == "the same words":
        assert whole_form(answer.text, action)["text"] == WORDS
    assert after == before


@pytest.mark.parametrize("surface", ["new", "edit"])
@pytest.mark.parametrize("held", ["2026-08-25", "20260825", "next week", ""])
def test_save_without_the_date_saves_without_it_whatever_the_date_control_holds(
    surface: str, held: str
) -> None:
    """The button does what it says. A day typed into the control beside it, one that reads
    or one that does not, is not saved by the button that says it will not be."""
    with browser() as client:
        store = state_of(client).project_state
        action, fields = opened_form(client, surface)
        fields.update(text="Words for this note", course="Math")
        refused = client.post(action, data={**fields, "due_date": "friday"}, headers=PAGE_HEADERS)
        marked = whole_form(refused.text, action)
        saved = client.post(
            action,
            data={**marked, "due_date": held, "choice": "without_date"},
            headers=PAGE_HEADERS,
        )
        note = store.capture(marked.get("capture_id") or action.split("/")[-2])

    assert marked["date_pending"] == "1"
    assert saved.status_code == 303
    assert note is not None
    assert (note.text, note.course, note.due_date) == ("Words for this note", "Math", None)
    assert "due_date" not in note.attribution


@pytest.mark.parametrize("held", ["2026-08-25", "next week"])
def test_a_refusal_beside_that_choice_keeps_the_day_she_typed_and_the_choice_to_make_again(
    held: str,
) -> None:
    """Pressed with words that are refused, nothing is saved, and the form that comes back
    still holds what she typed for the day and still offers both buttons: the plain one saves
    a day that reads, and the other one saves without it."""
    with browser() as client:
        store = state_of(client).project_state
        fields = new_form(client)
        long_words = "w" * (CAPTURE_TEXT_MAX_LENGTH + 1)
        refused = send(client, fields, text=WORDS, course="", due_date="friday")
        marked = whole_form(refused.text, NOTE_ACTIONS)
        sent = {**marked, "text": long_words, "due_date": held}
        too_long = send(client, sent, choice="without_date")
        returned = whole_form(too_long.text, NOTE_ACTIONS)
        nothing_yet = tables(store)
        plain = send(client, {**returned, "text": WORDS}, choice="save")
        note = store.capture(fields["capture_id"])

    assert too_long.status_code == 422
    assert said_first(too_long.text, NOTE_TOO_LONG)
    assert ">Save without the date</button>" in too_long.text
    assert nothing_yet == ([], [])
    if held == "2026-08-25":
        assert returned["due_date"] == "2026-08-25"
        assert plain.status_code == 303
        assert note is not None
        assert note.due_date == date(2026, 8, 25)
    else:
        assert (returned["due_date"], returned["date_pending"]) == ("", "1")
        assert returned["date_refused"] == "next week"
        assert plain.status_code == 422
        assert said_first(plain.text, NOTE_NEEDS_A_DATE_CHOICE)
        assert note is None


# ------------------------------------------------------- forms that are not whole


def test_a_form_that_is_not_the_one_the_page_made_writes_nothing_and_keeps_her_words() -> None:
    with browser() as client:
        store = state_of(client).project_state
        fields = new_form(client)
        name = fields["capture_id"]

        twice = client.post(
            NOTE_ACTIONS,
            content=f"capture_id={name}&text=one&text=two&course=&due_date=",
            headers={**PAGE_HEADERS, "Content-Type": "application/x-www-form-urlencoded"},
        )
        extra = send(client, fields, text=WORDS, course="", due_date="", role="parent")
        filed = client.post(
            NOTE_ACTIONS,
            data={"capture_id": name, "course": "", "due_date": ""},
            files={"text": ("note.txt", b"from a file", "text/plain")},
            headers=PAGE_HEADERS,
        )
        no_id = send(client, {**fields, "capture_id": "note-1"}, text=WORDS, course="", due_date="")
        written = tables(store)

    for answer in (twice, extra, filed, no_id):
        assert answer.status_code == 422
        assert said_first(answer.text, BAD_FORM)
    assert escape(WORDS) in extra.text
    assert ">one</textarea>" in twice.text
    assert capture_id_from(form_fields(no_id.text, NOTE_ACTIONS)["capture_id"])
    assert written == ([], [])


@pytest.mark.parametrize(
    ("typed", "sentence"),
    [
        ({"text": ""}, NOTE_NEEDS_WORDS),
        ({"text": "   "}, NOTE_NEEDS_WORDS),
        ({"text": "x" * 501}, NOTE_TOO_LONG),
        ({"text": "a bell" + chr(7)}, "That has a character Blossom cannot keep."),
        ({"text": WORDS, "course": "c" * 61}, "Keep the class to 60 characters or fewer."),
    ],
    ids=["blank", "only space", "501", "control", "course 61"],
)
def test_words_the_record_will_not_keep_are_said_beside_their_field(
    typed: dict[str, str], sentence: str
) -> None:
    with browser() as client:
        store = state_of(client).project_state
        fields = new_form(client)

        refused = send(
            client, fields, **{"text": WORDS, "course": "Geometry", "due_date": "", **typed}
        )
        written = tables(store)

    assert refused.status_code == 422
    assert str(escape(sentence)) in refused.text
    assert 'aria-invalid="true"' in refused.text
    assert form_fields(refused.text, NOTE_ACTIONS)["capture_id"] == fields["capture_id"]
    assert written == ([], [])


# ------------------------------------------------------ edit, archive, restore


def test_an_edit_is_saved_by_revision_and_a_page_that_is_behind_keeps_her_words() -> None:
    with browser() as client:
        store = state_of(client).project_state
        name = saved_note(client, course="Geometry")
        edit = note_action(name, "edit")
        opened = client.get(note_href(name, edit="1")).text
        behind = form_fields(opened, edit)

        saved = client.post(
            edit,
            data={**behind, "text": "Questions 4-9", "course": "Geometry", "due_date": ""},
            headers=PAGE_HEADERS,
        )
        after = follow(client, saved)
        same = follow(
            client,
            client.post(
                edit,
                data={**behind, "text": "Questions 4-9", "course": "Geometry", "due_date": ""},
                headers=PAGE_HEADERS,
            ),
        )
        stale = client.post(
            edit,
            data={**behind, "text": "From the <old> page", "course": "", "due_date": ""},
            headers=PAGE_HEADERS,
        )
        retry = form_fields(stale.text, edit)
        note = store.capture(name)

    assert behind["revision"] == "1"
    assert escape(NOTE_EDITED) in after
    assert "Questions 4-9" in after
    assert "First written as" in after
    assert escape(WORDS) in after
    assert escape(NOTE_ALREADY_SAVED) in same
    assert stale.status_code == 409
    assert said_first(stale.text, NOTE_CHANGED)
    assert "From the &lt;old&gt; page" in stale.text
    assert "Your unsaved changes" in stale.text
    assert retry["revision"] == "2"
    assert note is not None
    assert (note.text, note.course, note.revision) == ("Questions 4-9", "Geometry", 2)


def test_archive_and_restore_move_a_note_between_the_two_lists_and_keep_its_place() -> None:
    with browser() as client:
        first = saved_note(client, "first note")
        second = saved_note(client, "second note")
        archive, restore = note_action(first, "archive"), note_action(first, "restore")
        page = client.get(note_href(first)).text

        archived = follow(
            client, client.post(archive, data=form_fields(page, archive), headers=PAGE_HEADERS)
        )
        stale = client.post(archive, data={"revision": "7"}, headers=PAGE_HEADERS)
        waiting = client.get(NOTES_PAGE).text
        put_away = client.get(ARCHIVED_NOTES_PAGE).text
        restored = follow(
            client, client.post(restore, data=form_fields(archived, restore), headers=PAGE_HEADERS)
        )
        back = client.get(NOTES_PAGE).text
        edit_while_archived = client.get(note_href(first, edit="1")).text

    assert escape(NOTE_ARCHIVED) in archived
    assert "This note is archived." in archived
    assert stale.status_code == 303
    assert "first note" not in waiting
    assert "second note" in waiting
    assert "first note" in put_away
    assert escape(NOTE_RESTORED) in restored
    assert back.index("first note") < back.index("second note")
    assert second in back
    assert "first note" in edit_while_archived


def test_a_change_from_a_page_that_is_behind_is_refused_with_the_note_as_it_stands() -> None:
    with browser() as client:
        store = state_of(client).project_state
        name = saved_note(client)
        edit, archive = note_action(name, "edit"), note_action(name, "archive")
        behind = form_fields(client.get(note_href(name)).text, archive)
        opened = form_fields(client.get(note_href(name, edit="1")).text, edit)
        follow(
            client,
            client.post(
                edit,
                data={**opened, "text": "Newer words", "course": "", "due_date": ""},
                headers=PAGE_HEADERS,
            ),
        )

        refused = client.post(archive, data=behind, headers=PAGE_HEADERS)
        note = store.capture(name)

    assert refused.status_code == 409
    assert said_first(refused.text, NOTE_CHANGED)
    assert "Newer words" in refused.text
    assert form_fields(refused.text, archive)["revision"] == "2"
    assert note is not None
    assert (note.archived, note.revision) == (False, 2)


# ------------------------------------------------- results that stay true


def test_a_result_read_after_the_note_moved_on_says_so_and_a_made_up_one_says_nothing() -> None:
    with browser() as client:
        store = state_of(client).project_state
        fields = new_form(client)
        held = send(client, fields, text=WORDS, course="", due_date="").headers["location"]
        name = fields["capture_id"]
        edit = note_action(name, "edit")
        opened = form_fields(client.get(note_href(name, edit="1")).text, edit)
        follow(
            client,
            client.post(
                edit,
                data={**opened, "text": "Edited elsewhere", "course": "", "due_date": ""},
                headers=PAGE_HEADERS,
            ),
        )

        later = client.get(held).text
        first, second = (event.event_id for event in store.capture_history(name))
        other = saved_note(client, "Another note")
        elsewhere = store.capture_history(other)[0].event_id
        made_up = [
            client.get(note_href(name, said="archived", event=first)).text,
            client.get(note_href(name, said="saved", event=second)).text,
            client.get(note_href(name, said="edited", event=first)).text,
            client.get(note_href(name, said="saved", event="note-event-000000000000")).text,
            client.get(note_href(name, said="everything", event=first)).text,
            client.get(note_href(name, said="same", event="2")).text,
            client.get(note_href(name, said="same", rev="2")).text,
            client.get(note_href(name, said="unchanged", rev="2")).text,
            client.get(note_href(name, said="unchanged", event=elsewhere)).text,
            client.get(note_href(name, said="same", event="e" * 500)).text,
            client.get(note_href(name, said="same")).text,
        ]
        rows = tables(store)

    assert "said=saved" in held
    assert f"event={first}" in held
    assert "rev=" not in held
    assert escape(NOTE_SAVED_EARLIER) in later
    assert escape(NOTE_SAVED) not in later
    assert "Edited elsewhere" in later
    for page in made_up:
        assert 'id="note-result"' not in page
        assert escape(NOTE_ALREADY_SAVED) not in page
    assert len(rows[1]) == 3


def test_a_save_that_wrote_nothing_is_said_from_the_change_it_found_and_no_other() -> None:
    """The address of an already saved result names the change the save found standing, which
    only the server knows; while that is the latest it is said, and after another change it
    is said as something done earlier."""
    with browser() as client:
        store = state_of(client).project_state
        fields = new_form(client)
        typed = {"text": WORDS, "course": "", "due_date": ""}
        send(client, fields, **typed)
        name = fields["capture_id"]
        replay = send(client, fields, **typed).headers["location"]
        edit = note_action(name, "edit")
        opened = form_fields(client.get(note_href(name, edit="1")).text, edit)
        no_change = client.post(edit, data={**opened, **typed}, headers=PAGE_HEADERS)
        unchanged = no_change.headers["location"]
        first = store.capture_history(name)[0].event_id
        said_now = [client.get(replay).text, client.get(unchanged).text]
        client.post(edit, data={**opened, **typed, "text": "Edited since"}, headers=PAGE_HEADERS)
        said_later = [client.get(replay).text, client.get(unchanged).text]
        events = tables(store)[1]

    assert no_change.status_code == 303
    for where, said in ((replay, "same"), (unchanged, "unchanged")):
        assert f"said={said}" in where
        assert f"event={first}" in where
        assert "rev=" not in where
    for page in said_now:
        assert escape(NOTE_ALREADY_SAVED) in page
    for page in said_later:
        assert escape(NOTE_SAVED_EARLIER) in page
        assert escape(NOTE_ALREADY_SAVED) not in page
        assert "Edited since" in page
    assert len(events) == 2


def test_a_note_that_cannot_be_read_is_said_so_wherever_it_is_opened_and_never_as_gone() -> None:
    """On record and unreadable is not the same as not on record: the help page and a request
    for help say the first, as the note's own page does, and nothing is sent. A change that
    cannot be read makes the note's page say the same and never fail."""
    with browser() as client:
        state = state_of(client)
        store = state.project_state
        name = saved_note(client)
        damaged = saved_note(client, "A note whose history is damaged")
        store._connection.execute(
            "UPDATE homework_captures SET due_date = 'next week' WHERE capture_id = ?", (name,)
        )
        store._connection.execute(
            "UPDATE capture_events SET occurred_on = 'someday' WHERE capture_id = ?", (damaged,)
        )
        store._connection.commit()

        opened = client.get(note_help_href(name))
        asked = client.post(
            note_action(name, "ask-for-help"), data={"note": "which part?"}, headers=PAGE_HEADERS
        )
        page = client.get(note_href(damaged))
        sent = state.help_requests.open_requests()

    for answer, status_code in ((opened, 200), (asked, 500), (page, 200)):
        assert answer.status_code == status_code
        assert escape(NOTE_UNREADABLE) in answer.text
        assert escape(NOTE_GONE) not in answer.text
        assert WORDS not in answer.text
    assert sent == []


# ------------------------------------------------------------------- who may


def test_a_parent_reads_every_note_and_changes_none(tmp_path: pathlib.Path) -> None:
    app = create_app(signed_in_household(tmp_path))
    with TestClient(app, follow_redirects=False, headers=SAME_ORIGIN) as client:
        client.post("/sign-in", data={"passphrase": HERS})
        store = state_of(client).project_state
        kept = saved_note(client, "a note she kept", course="Geometry")
        put_away = saved_note(client, "a note she put away")
        page = client.get(note_href(put_away)).text
        archive = note_action(put_away, "archive")
        client.post(archive, data=form_fields(page, archive), headers=PAGE_HEADERS)
        hers = client.get(note_href(kept)).text
        authors = [event.authored_by for event in store.capture_history(kept)]
        client.post("/sign-out")
        client.post("/sign-in", data={"passphrase": THEIRS})
        before = tables(store)

        reads = [
            client.get(note_href(kept)),
            client.get(note_href(put_away)),
            client.get(NOTES_PAGE),
            client.get(ARCHIVED_NOTES_PAGE),
            client.get(NEW_NOTE_PAGE),
            client.get(note_href(kept, edit="1")),
        ]
        writes = [
            send(
                client,
                {"capture_id": new_capture_id()},
                text="from a parent",
                course="",
                due_date="",
            ),
            client.post(
                note_action(kept, "edit"),
                data={"revision": "1", "text": "rewritten", "course": "", "due_date": ""},
                headers=PAGE_HEADERS,
            ),
            client.post(note_action(kept, "archive"), data={"revision": "1"}, headers=PAGE_HEADERS),
            client.post(
                note_action(put_away, "restore"), data={"revision": "2"}, headers=PAGE_HEADERS
            ),
            client.post(
                note_action(kept, "ask-for-help"), data={"note": "for her"}, headers=PAGE_HEADERS
            ),
        ]
        after = tables(store)
        asked = state_of(client).help_requests.open_requests()

    assert authors == [STUDENT]
    assert "Edit this note" in hers
    assert [answer.status_code for answer in reads] == [200] * 6
    for answer in reads:
        assert '<form method="post"' not in answer.text.split('<main id="main"', 1)[1]
    assert "a note she kept" in reads[0].text
    assert "History of this note" in reads[0].text
    assert "a note she put away" in reads[1].text
    assert "a note she put away" in reads[3].text
    assert "Sign in as the student" in reads[4].text
    assert [answer.status_code for answer in writes] == [403] * 5
    assert all(escape(NOT_HERS_TO_UPDATE) in answer.text for answer in writes)
    assert after == before
    assert asked == []


def test_a_name_that_is_no_note_is_a_small_404_and_is_looked_up_nowhere() -> None:
    with browser() as client:
        saved_note(client)
        answers = [
            client.get(f"{NOTES_PAGE}/note-1"),
            client.get(note_href(new_capture_id())),
            client.get(f"{NOTES_PAGE}/{new_capture_id().upper()}"),
            client.post(
                note_action(new_capture_id(), "archive"),
                data={"revision": "1"},
                headers=PAGE_HEADERS,
            ),
            client.get(f"{NOTES_PAGE}/{new_capture_id()}/help"),
        ]

    assert [answer.status_code for answer in answers] == [404] * 5
    for answer in answers:
        assert "This homework note is not on record." in answer.text
        assert WORDS not in html.unescape(answer.text)
        assert f'href="{NOTES_PAGE}"' in answer.text


# ------------------------------------------------- a write and then a read fail


def test_a_save_the_file_refuses_is_said_even_when_nothing_can_be_read_back(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    with browser() as client:
        store = state_of(client).project_state
        fields = new_form(client)
        tried: list[str] = []

        def refuses(*args: object, **kwargs: object) -> None:
            tried.append("write")
            msg = "the file refused"
            raise sqlite3.OperationalError(msg)

        monkeypatch.setattr(store, "_append_capture_event_locked", refuses)
        readable = send(client, fields, text="<i>my</i> words", course="Geometry", due_date="")

        def unread(*args: object, **kwargs: object) -> None:
            msg = "the file cannot be read"
            raise sqlite3.OperationalError(msg)

        monkeypatch.setattr(store, "capture", unread)
        monkeypatch.setattr(store, "outstanding_captures", unread)
        plain = send(client, fields, text="<i>my</i> words", course="Geometry", due_date="")
        monkeypatch.undo()
        written = tables(store)

    for answer in (readable, plain):
        assert answer.status_code == 500
        assert str(escape(NOTE_NOT_SAVED)) in answer.text
        assert "&lt;i&gt;my&lt;/i&gt; words" in answer.text
        assert "<i>my</i>" not in answer.text
        assert answer.text.count(" autofocus") == 1
        assert escape(NOTE_SAVED) not in answer.text
        assert f'href="{HER_PAGE}"' in answer.text
    assert tried == ["write", "write"]
    assert written == ([], [])
