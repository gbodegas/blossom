"""Words she or a parent typed stay out of every error and log line about a row that can't
be read.

Her question for help, her update and hand-in notes, and a parent's check note are kept in
rows a store decodes. When a row can't be decoded, the store's error names the record, the
field and the kind of refusal, with no cause or context, and a save refused over it says the
same. Each page that logs such a failure logs none of the words, the traceback included.
"""

import contextlib
import logging
import pathlib
import sqlite3
import traceback
import uuid
from collections.abc import Callable
from datetime import UTC, date, datetime
from typing import NamedTuple

import pytest
from fastapi.testclient import TestClient

from blossom.hand_in import NEEDS_HAND_IN, TURNED_IN, HandInSaved
from blossom.routes.navigation import TO_TURN_IN_PAGE, note_action
from blossom.stores.help_requests import HelpRequestsStore, UnreadableHelpRequest
from blossom.stores.project_state import (
    Checked,
    CouldNotSave,
    ProjectStateStore,
    Reopened,
    Saved,
    StudentReport,
    UnreadableEvent,
)
from tests.support import (
    ESSAY_ID,
    FIXTURE_WEEK,
    HER_PAGE,
    MISSING_EMAIL,
    PAGE_HEADERS,
    PLAN_DATE,
    PRACTICE,
    PRACTICE_LOG,
    browser,
    card_for,
    fixture_clock,
    form_fields,
    hidden,
    practice_store,
    refusing,
    report,
    state_of,
    waiting_note,
    whole_form,
)
from tests.support import SAID_AT as NOW
from tests.support import SAID_ON as TODAY

QUESTION = "Walrus marimba question"
UPDATE = "Ocelot zither update"
HAND_IN = "Narwhal bassoon handed"
CHECK = "Tapir oboe checked"
TYPED = ("Walrus", "marimba", "Ocelot", "zither", "Narwhal", "bassoon", "Tapir", "oboe")
"""Every word typed in this module. No error or log line may hold one."""
DETAILS = f"/student/assignments/{ESSAY_ID}"
ACTIONS = f"/student/actions/assignments/{ESSAY_ID}"


def words_in(text: str) -> list[str]:
    return [word for word in TYPED if word in text]


def told(error: BaseException) -> str:
    """An error as a server's log prints it: its message, traceback, causes and context."""
    return "".join(traceback.format_exception(error))


def logged(records: list[logging.LogRecord]) -> str:
    """Every record as a handler prints it: its message, its args and its traceback."""
    formatter = logging.Formatter()
    return "\n".join(formatter.format(record) + repr(record.args) for record in records)


def failures(records: list[logging.LogRecord]) -> list[tuple[str, str, str]]:
    """Each record logged with a traceback: its logger, its message and its error's kind."""
    return [
        (record.name, record.getMessage(), type(record.exc_info[1]).__name__)
        for record in records
        if record.exc_info is not None
    ]


def damage(client: TestClient, statement: str) -> None:
    store = state_of(client).project_state
    store._connection.execute(statement)
    store._connection.commit()


DAMAGES = {
    "past the limit": {
        "help_requests": "UPDATE help_requests SET note = note || printf('%.*c', 600, 'x')",
        "student_reports": "UPDATE student_reports SET note = note || printf('%.*c', 600, 'x')",
        "hand_in_events": "UPDATE hand_in_events SET note = note || printf('%.*c', 600, 'x')",
        "family_checks": "UPDATE family_checks SET note = note || printf('%.*c', 600, 'x')",
    },
    "not UTF-8": {
        "help_requests": (
            "UPDATE help_requests SET note = CAST(CAST(note AS BLOB) || x'ff' AS TEXT)"
        ),
        "student_reports": (
            "UPDATE student_reports SET note = CAST(CAST(note AS BLOB) || x'ff' AS TEXT)"
        ),
        "hand_in_events": (
            "UPDATE hand_in_events SET note = CAST(CAST(note AS BLOB) || x'ff' AS TEXT)"
        ),
        "family_checks": (
            "UPDATE family_checks SET note = CAST(CAST(note AS BLOB) || x'ff' AS TEXT)"
        ),
    },
}
"""Two ways the words in each table can't be read: past their limit, or not UTF-8."""
REFUSED_AS = {"past the limit": "UnreadableHelpRequest", "not UTF-8": "OperationalError"}
"""The error a help request damaged each way is read as."""


# ------------------------------------------------------------------ the stores


NOT_UTF_8 = "a text column holds bytes that are not UTF-8"
"""What a read says of a text column whose bytes aren't UTF-8, where sqlite3's own refusal
quotes the column."""
AT_THE_LIMIT = "y" * 500
"""A neighbor's words at the limit each of these notes is held to."""

UNREAD_REQUESTS: dict[str, tuple[str, type[Exception], str]] = {
    "her words past the limit": (
        "UPDATE help_requests SET note = note || printf('%.*c', 600, 'x') WHERE request_id = ?",
        UnreadableHelpRequest,
        "the help request {id} cannot be read: HelpRequest: note string_too_long",
    ),
    "a word back past the limit": (
        "UPDATE help_requests SET response = note || printf('%.*c', 600, 'x') WHERE request_id = ?",
        UnreadableHelpRequest,
        "the help request {id} cannot be read: HelpRequest: response string_too_long",
    ),
    "her words in place of a day": (
        "UPDATE help_requests SET evening = note WHERE request_id = ?",
        UnreadableHelpRequest,
        "the help request {id} cannot be read: ValueError",
    ),
    "her words in place of its id and its state": (
        "UPDATE help_requests SET request_id = note, state = note WHERE request_id = ?",
        UnreadableHelpRequest,
        "a kept help request cannot be read: HelpRequest: state literal_error",
    ),
    "her words not UTF-8": (
        "UPDATE help_requests SET note = CAST(CAST(note AS BLOB) || x'ff' AS TEXT) "
        "WHERE request_id = ?",
        sqlite3.OperationalError,
        NOT_UTF_8,
    ),
}
"""Damage to a kept request, and what its error says: the request by an id in the shape a
form carries, never one the row made up, and the field and kind of refusal."""


@pytest.mark.parametrize("damaged", list(UNREAD_REQUESTS))
def test_a_help_request_that_cannot_be_read_is_named_without_her_words(damaged: str) -> None:
    store = HelpRequestsStore(sqlite3.connect(":memory:", check_same_thread=False), fixture_clock())
    asked = store.ask(PLAN_DATE, QUESTION)
    neighbor = store.ask(PLAN_DATE, AT_THE_LIMIT)
    statement, refused_as, said = UNREAD_REQUESTS[damaged]
    store._connection.execute(statement, (asked.request_id,))
    store._connection.commit()

    with pytest.raises(refused_as) as listed:
        store.open_requests()
    with pytest.raises(refused_as) as held:
        store.retained()
    kept = store.get(neighbor.request_id)
    taken_back = store.take_back(neighbor.request_id)

    assert str(listed.value) == said.format(id=asked.request_id)
    for error in (listed.value, held.value):
        assert (error.__cause__, error.__context__) == (None, None)
        assert words_in(told(error)) == []
    assert kept == neighbor
    assert taken_back is True


def an_update_on(store: ProjectStateStore, assignment_id: str, note: str) -> str:
    saved = store.report_status(
        assignment_id, "done", note, expected_head=None, now=NOW, today=TODAY
    )
    assert isinstance(saved, Saved)
    return saved.report.report_id


def a_hand_in_on(store: ProjectStateStore, assignment_id: str, note: str) -> str:
    kept = store.record_hand_in(
        assignment_id, TURNED_IN, None, note, expected_head=None, now=NOW, today=TODAY
    )
    assert isinstance(kept, HandInSaved)
    return kept.event.event_id


def a_check_on(store: ProjectStateStore, assignment_id: str, note: str) -> str:
    kept = store.mark_checked(
        assignment_id,
        "basis",
        note,
        expected_check=None,
        basis_now=lambda: "basis",
        now=NOW,
        today=TODAY,
    )
    assert isinstance(kept, Checked)
    return kept.check.check_id


def an_update(store: ProjectStateStore) -> str:
    return an_update_on(store, PRACTICE, UPDATE)


def a_hand_in(store: ProjectStateStore) -> str:
    return a_hand_in_on(store, PRACTICE, HAND_IN)


def a_check(store: ProjectStateStore) -> str:
    return a_check_on(store, PRACTICE, CHECK)


class Kind(NamedTuple):
    """One kind of event: how one is made with its words, a read of every row or of the
    assignments named, a save over the head of one and what it makes, the two damages to
    the practice's row, the model that refuses it, and what a refused save names."""

    made: Callable[[ProjectStateStore, str, str], str]
    words: str
    read: Callable[[ProjectStateStore, list[str] | None], dict[str, object]]
    saved_over: Callable[[ProjectStateStore, str, str], object]
    saved_as: type
    past_the_limit: str
    not_utf_8: str
    model: str
    what: str


KINDS = {
    "her update": Kind(
        an_update_on,
        UPDATE,
        lambda store, named: dict(store.student_report_chains(named)),
        lambda store, assignment_id, head: store.report_status(
            assignment_id, "not_yet", None, expected_head=head, now=NOW, today=TODAY
        ),
        Saved,
        "UPDATE student_reports SET note = note || printf('%.*c', 600, 'x') "
        "WHERE assignment_id = 'assignment-practice'",
        "UPDATE student_reports SET note = CAST(CAST(note AS BLOB) || x'ff' AS TEXT) "
        "WHERE assignment_id = 'assignment-practice'",
        "StudentReport",
        "the update",
    ),
    "her hand-in": Kind(
        a_hand_in_on,
        HAND_IN,
        lambda store, named: dict(store.hand_in_chains(named)),
        lambda store, assignment_id, head: store.record_hand_in(
            assignment_id, NEEDS_HAND_IN, None, None, expected_head=head, now=NOW, today=TODAY
        ),
        HandInSaved,
        "UPDATE hand_in_events SET note = note || printf('%.*c', 600, 'x') "
        "WHERE assignment_id = 'assignment-practice'",
        "UPDATE hand_in_events SET note = CAST(CAST(note AS BLOB) || x'ff' AS TEXT) "
        "WHERE assignment_id = 'assignment-practice'",
        "HandInEvent",
        "the update",
    ),
    "a parent's check": Kind(
        a_check_on,
        CHECK,
        lambda store, named: dict(store.family_check_chains(named)),
        lambda store, assignment_id, head: store.check_again(
            assignment_id,
            head,
            expected_basis="basis",
            basis_now=lambda: "basis",
            now=NOW,
            today=TODAY,
        ),
        Reopened,
        "UPDATE family_checks SET note = note || printf('%.*c', 600, 'x') "
        "WHERE assignment_id = 'assignment-practice'",
        "UPDATE family_checks SET note = CAST(CAST(note AS BLOB) || x'ff' AS TEXT) "
        "WHERE assignment_id = 'assignment-practice'",
        "FamilyCheck",
        "the check",
    ),
}


@pytest.mark.parametrize("damaged", ["past the limit", "not UTF-8"])
@pytest.mark.parametrize("kind", list(KINDS))
def test_an_event_that_cannot_be_read_is_named_without_the_words_it_keeps(
    kind: str, damaged: str, tmp_path: pathlib.Path
) -> None:
    event = KINDS[kind]
    past = damaged == "past the limit"
    store = practice_store(tmp_path / "blossom.sqlite3")
    try:
        head = event.made(store, PRACTICE, event.words)
        neighbor = event.made(store, PRACTICE_LOG, AT_THE_LIMIT)
        store._connection.execute(event.past_the_limit if past else event.not_utf_8)
        store._connection.commit()
        with pytest.raises((UnreadableEvent, sqlite3.OperationalError)) as unread:
            event.read(store, None)
        with pytest.raises(CouldNotSave) as refused:
            event.saved_over(store, PRACTICE, head)
        neighbors = event.read(store, [PRACTICE_LOG])
        saved = event.saved_over(store, PRACTICE_LOG, neighbor)
    finally:
        store.close()

    said = f"{head} cannot be read: {event.model}: note value_error" if past else NOT_UTF_8
    assert type(unread.value) is (UnreadableEvent if past else sqlite3.OperationalError)
    assert str(unread.value) == said
    assert (unread.value.__cause__, unread.value.__context__) == (None, None)
    assert str(refused.value) == f"{event.what} on {PRACTICE!r} could not be saved: {said}"
    assert type(refused.value.__cause__) is type(unread.value)
    assert refused.value.__context__ is None
    for error in (unread.value, refused.value):
        assert words_in(told(error)) == []
    assert list(neighbors) == [PRACTICE_LOG]
    assert isinstance(saved, event.saved_as)


def test_an_event_whose_id_holds_her_words_is_named_by_no_id(tmp_path: pathlib.Path) -> None:
    store = practice_store(tmp_path / "blossom.sqlite3")
    try:
        an_update(store)
        store._connection.execute(
            "UPDATE student_reports SET report_id = note, note = note || printf('%.*c', 600, 'x')"
        )
        store._connection.commit()
        with pytest.raises(UnreadableEvent) as unread:
            store.student_report_chains()
    finally:
        store.close()

    assert str(unread.value) == "an event cannot be read: StudentReport: note value_error"


def nothing_made(store: ProjectStateStore) -> str:
    return ""


class Save(NamedTuple):
    """One save of an event: what it saves over, the save, the step that appends the
    event, and what a refused save names."""

    over: Callable[[ProjectStateStore], str]
    saved: Callable[[ProjectStateStore, str], object]
    appended_by: str
    what: str


SAVES = {
    "her update": Save(
        nothing_made,
        lambda store, _: store.report_status(
            PRACTICE, "done", None, expected_head=None, now=NOW, today=TODAY
        ),
        "_append_student_report_locked",
        "the update",
    ),
    "her update undone": Save(
        an_update,
        lambda store, head: store.undo_report(PRACTICE, head, now=NOW, today=TODAY),
        "_append_student_report_locked",
        "the update",
    ),
    "her hand-in": Save(
        nothing_made,
        lambda store, _: store.record_hand_in(
            PRACTICE, TURNED_IN, None, None, expected_head=None, now=NOW, today=TODAY
        ),
        "_append_hand_in_locked",
        "the update",
    ),
    "her hand-in undone": Save(
        a_hand_in,
        lambda store, head: store.undo_hand_in(PRACTICE, head, now=NOW, today=TODAY),
        "_append_hand_in_locked",
        "the update",
    ),
    "a check": Save(
        nothing_made,
        lambda store, _: store.mark_checked(
            PRACTICE,
            "basis",
            None,
            expected_check=None,
            basis_now=lambda: "basis",
            now=NOW,
            today=TODAY,
        ),
        "_append_family_check_locked",
        "the check",
    ),
    "a check reopened": Save(
        a_check,
        lambda store, head: store.check_again(
            PRACTICE,
            head,
            expected_basis="basis",
            basis_now=lambda: "basis",
            now=NOW,
            today=TODAY,
        ),
        "_append_family_check_locked",
        "the check",
    ),
}


def refused_by_a_model(*_: object) -> None:
    """A model's refusal while the event is appended, with her words as its input."""
    StudentReport.model_validate(
        {
            "report_id": "report-000000000000",
            "assignment_id": PRACTICE,
            "operation": "report",
            "status": "done",
            "note": UPDATE + "x" * 600,
            "reported_at": NOW,
            "reported_on": TODAY,
        }
    )


@pytest.mark.parametrize("save", list(SAVES))
def test_a_save_a_model_refuses_is_named_without_what_it_was_sent(
    save: str, tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    saving = SAVES[save]
    store = practice_store(tmp_path / "blossom.sqlite3")
    try:
        head = saving.over(store)
        monkeypatch.setattr(store, saving.appended_by, refused_by_a_model)
        with pytest.raises(CouldNotSave) as refusal:
            saving.saved(store, head)
    finally:
        store.close()

    assert str(refusal.value) == (
        f"{saving.what} on {PRACTICE!r} could not be saved: StudentReport: note value_error"
    )
    assert (refusal.value.__cause__, refusal.value.__context__) == (None, None)
    assert words_in(told(refusal.value)) == []


# ------------------------------------------------------------------ the pages


HELP_FORMS = {
    "her week's": ("blossom.routes.student", "her request for help could not be sent"),
    "a note's": (
        "blossom.routes.captures",
        "the form for her request for help about note {name} was not read",
    ),
    "a note's, asked between its two checks": (
        "blossom.routes.captures",
        "her request for help about note {name} could not be sent",
    ),
}
"""A help form sent again over its request, and the failure its page logs."""


@pytest.mark.parametrize("damaged", list(DAMAGES))
@pytest.mark.parametrize("form", list(HELP_FORMS))
def test_a_help_form_sent_again_over_a_request_that_cannot_be_read_logs_none_of_her_words(
    form: str, damaged: str, caplog: pytest.LogCaptureFixture, monkeypatch: pytest.MonkeyPatch
) -> None:
    caplog.set_level(logging.DEBUG)
    with browser() as client:
        state = state_of(client)
        name = waiting_note(
            state.project_state, course="Geometry", title="Questions 4-8", text="Page 12"
        )
        action = (
            "/student/actions/ask-for-help"
            if form == "her week's"
            else note_action(name, "ask-for-help")
        )
        sent = {"note": QUESTION, "request_id": uuid.uuid4().hex}
        assert client.post(action, data=sent, headers=PAGE_HEADERS).status_code == 303
        state.help_requests._connection.execute(DAMAGES[damaged]["help_requests"])
        state.help_requests._connection.commit()
        if form.endswith("checks"):

            def unused(*_: object, **__: object) -> None:
                return None

            monkeypatch.setattr(state.help_requests, "already_asked", unused)
        caplog.clear()
        answer = client.post(action, data=sent, headers=PAGE_HEADERS)
        records = list(caplog.records)

    logger, message = HELP_FORMS[form]
    assert answer.status_code == 500
    assert failures(records) == [(logger, message.format(name=name), REFUSED_AS[damaged])]
    assert words_in(logged(records)) == []


@pytest.mark.parametrize("damaged", list(DAMAGES))
@pytest.mark.parametrize("press", ["saved", "undone"])
def test_her_update_pressed_over_one_that_cannot_be_read_logs_none_of_her_words(
    press: str, damaged: str, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.DEBUG)
    with browser() as client:
        report(client, ESSAY_ID, "done", UPDATE)
        week = {"week": FIXTURE_WEEK}
        changing = client.get(HER_PAGE, params={**week, "change": ESSAY_ID}).text
        shown = client.get(HER_PAGE, params={**week, "show": ESSAY_ID}).text
        damage(client, DAMAGES[damaged]["student_reports"])
        caplog.clear()
        if press == "saved":
            head = hidden(card_for(changing, ESSAY_ID), "expected_report_id")
            sent = {"status": "not_yet", "note": "", "expected_report_id": head, **week}
            client.post(f"{ACTIONS}/report", data=sent)
        else:
            undo = {"report_id": hidden(card_for(shown, ESSAY_ID), "report_id"), **week}
            client.post(f"{ACTIONS}/undo-report", data=undo)
        records = list(caplog.records)

    assert failures(records)[0] == (
        "blossom.routes.student",
        f"her update on {ESSAY_ID} could not be {press}",
        "CouldNotSave",
    )
    assert words_in(logged(records)) == []


def hand_in_form(client: TestClient) -> dict[str, str]:
    return form_fields(client.get(DETAILS, params={"hand_in": "change"}).text, f"{ACTIONS}/hand-in")


def handed_in(client: TestClient) -> dict[str, str]:
    """Her hand-in saved with a note from the details page; the form its undo sends."""
    sent = {**hand_in_form(client), "state": TURNED_IN, "next_action": "", "note": HAND_IN}
    saved = client.post(f"{ACTIONS}/hand-in", data=sent, headers=PAGE_HEADERS)
    assert saved.status_code == 303
    return form_fields(client.get(saved.headers["location"]).text, f"{ACTIONS}/undo-hand-in")


def a_hand_in_past_the_limit(client: TestClient) -> None:
    damage(client, DAMAGES["past the limit"]["hand_in_events"])


@pytest.mark.parametrize("damaged", list(DAMAGES))
@pytest.mark.parametrize("press", ["saved", "undone"])
def test_her_hand_in_pressed_over_one_that_cannot_be_read_logs_none_of_her_words(
    press: str, damaged: str, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.DEBUG)
    with browser() as client:
        undo = handed_in(client)
        behind = hand_in_form(client)
        damage(client, DAMAGES[damaged]["hand_in_events"])
        caplog.clear()
        if press == "saved":
            sent = {**behind, "state": NEEDS_HAND_IN, "next_action": "", "note": ""}
            client.post(f"{ACTIONS}/hand-in", data=sent, headers=PAGE_HEADERS)
        else:
            client.post(f"{ACTIONS}/undo-hand-in", data=undo, headers=PAGE_HEADERS)
        records = list(caplog.records)

    assert failures(records)[0] == (
        "blossom.routes.hand_in",
        f"her hand-in update on {ESSAY_ID} could not be {press}",
        "CouldNotSave",
    )
    assert words_in(logged(records)) == []


def check_row(client: TestClient) -> str:
    """The essay's row on the family page, from its id to the end of the page."""
    page = client.get("/parent", headers=PAGE_HEADERS).text
    return page[page.index(f'id="update-{ESSAY_ID}"') :]


@pytest.mark.parametrize("damaged", list(DAMAGES))
@pytest.mark.parametrize(("press", "said"), [("mark", "saved"), ("again", "reopened")])
def test_a_check_pressed_over_one_that_cannot_be_read_logs_none_of_its_words(
    press: str, said: str, damaged: str, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.DEBUG)
    with browser() as client:
        assert client.post("/parent/inbox/keep", data={"text": MISSING_EMAIL}).status_code == 303
        report(client, ESSAY_ID, "done", "Handed in Tuesday.")
        first = check_row(client)
        mark = {
            "basis": hidden(first, "basis"),
            "expected_check_id": hidden(first, "expected_check_id"),
        }
        marked = client.post(
            f"/parent/actions/checks/{ESSAY_ID}/mark",
            data={**mark, "note": CHECK},
            headers=PAGE_HEADERS,
        )
        assert marked.status_code == 303
        checked = check_row(client)
        again = {"check_id": hidden(checked, "check_id"), "basis": hidden(checked, "basis")}
        damage(client, DAMAGES[damaged]["family_checks"])
        caplog.clear()
        sent = {**mark, "note": ""} if press == "mark" else again
        client.post(f"/parent/actions/checks/{ESSAY_ID}/{press}", data=sent, headers=PAGE_HEADERS)
        records = list(caplog.records)

    assert failures(records)[0] == (
        "blossom.routes.parent",
        f"the check on {ESSAY_ID} could not be {said}",
        "CouldNotSave",
    )
    assert words_in(logged(records)) == []


def refused_help(client: TestClient, monkeypatch: pytest.MonkeyPatch) -> Callable[[], object]:
    monkeypatch.setattr(
        state_of(client).help_requests, "ask_once", refusing(sqlite3.OperationalError)
    )
    sent = {"note": "", "request_id": uuid.uuid4().hex}
    return lambda: client.post("/student/actions/ask-for-help", data=sent, headers=PAGE_HEADERS)


def refused_on_the_details(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> Callable[[], object]:
    handed_in(client)
    sent = {**hand_in_form(client), "state": NEEDS_HAND_IN, "next_action": "", "note": ""}
    return lambda: client.post(f"{ACTIONS}/hand-in", data=sent, headers=PAGE_HEADERS)


def refused_on_the_list(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> Callable[[], object]:
    at = datetime(2026, 8, 19, 21, 0, tzinfo=UTC)
    kept = state_of(client).project_state.record_hand_in(
        ESSAY_ID, NEEDS_HAND_IN, None, HAND_IN, expected_head=None, now=at, today=date(2026, 8, 19)
    )
    assert isinstance(kept, HandInSaved)
    page = client.get(TO_TURN_IN_PAGE).text
    row = page[page.index(f'id="to-turn-in-{ESSAY_ID}"') :]
    sent = form_fields(row, f"{ACTIONS}/hand-in")
    return lambda: client.post(f"{ACTIONS}/hand-in", data=sent, headers=PAGE_HEADERS)


def her_plan(client: TestClient, monkeypatch: pytest.MonkeyPatch) -> Callable[[], object]:
    return lambda: client.post("/student/actions/plan", headers=PAGE_HEADERS)


def the_familys_plan(client: TestClient, monkeypatch: pytest.MonkeyPatch) -> Callable[[], object]:
    sent = whole_form(client.get("/parent", headers=PAGE_HEADERS).text, "/parent/actions/plan")
    return lambda: client.post("/parent/actions/plan", data=sent, headers=PAGE_HEADERS)


READ_BACKS: dict[str, Callable[[TestClient, pytest.MonkeyPatch], Callable[[], object]]] = {
    "her request for help refused": refused_help,
    "her hand-in refused on the details": refused_on_the_details,
    "her hand-in refused on the list": refused_on_the_list,
    "her plan": her_plan,
    "the family's plan": the_familys_plan,
}
"""A press whose page is read again, or made, after it fails, with her update on the record.
Each sets up its press before the damage and makes it after."""


@pytest.mark.parametrize("damaged", list(DAMAGES))
@pytest.mark.parametrize("press", list(READ_BACKS))
def test_a_page_made_over_an_update_that_cannot_be_read_logs_none_of_her_words(
    press: str, damaged: str, caplog: pytest.LogCaptureFixture, monkeypatch: pytest.MonkeyPatch
) -> None:
    caplog.set_level(logging.DEBUG)
    with browser(key=True) as client:
        report(client, ESSAY_ID, "done", UPDATE)
        pressed = READ_BACKS[press](client, monkeypatch)
        damage(client, DAMAGES[damaged]["student_reports"])
        a_hand_in_past_the_limit(client)
        caplog.clear()
        # Whether the page stands over such a row is not asked here; what it logs is.
        with contextlib.suppress(UnreadableEvent, sqlite3.OperationalError):
            pressed()
        records = list(caplog.records)

    assert words_in(logged(records)) == []


@pytest.mark.parametrize("damaged", list(DAMAGES))
@pytest.mark.parametrize("path", ["/student/help-requests", "/parent/help-requests"])
def test_what_a_help_list_lets_out_over_a_request_that_cannot_be_read_holds_none_of_her_words(
    path: str, damaged: str
) -> None:
    escaped: BaseException | None = None
    with browser() as client:
        sent = {"note": QUESTION, "request_id": uuid.uuid4().hex}
        asked = client.post("/student/actions/ask-for-help", data=sent, headers=PAGE_HEADERS)
        assert asked.status_code == 303
        help_requests = state_of(client).help_requests
        help_requests._connection.execute(DAMAGES[damaged]["help_requests"])
        help_requests._connection.commit()
        try:
            client.get(path)
        except Exception as error:  # what a server would log for a failure no page catches
            escaped = error

    assert escaped is None or words_in(told(escaped)) == []
