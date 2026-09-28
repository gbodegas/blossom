"""One request for help per form: the id a form carries makes one request and never another,
taken back, resolved or gone. Only the ids are kept once a request's words are removed."""

import pathlib
import sqlite3
import threading
from datetime import UTC, date, datetime, timedelta

import pytest

from blossom.captures import STUDENT, CaptureCreated, new_capture_id
from blossom.clock import FrozenClock
from blossom.reconciliation import SourceChannel
from blossom.stores.help_requests import (
    HELP_RETENTION_DAYS,
    HelpAlreadyAsked,
    HelpAsked,
    HelpFormChanged,
    HelpFormUsed,
    HelpRequestsStore,
    NotARequestId,
    UnknownCaptureReference,
    new_request_id,
    request_id_from,
)
from blossom.stores.project_state import ProjectStateStore
from tests.support import PLAN_DATE, fixture_clock, practice_store

AT = datetime(2026, 9, 14, 21, 0, tzinfo=UTC)
MONDAY = date(2026, 9, 14)


def a_note(store: ProjectStateStore, text: str = "Geometry questions 4-8") -> str:
    name = new_capture_id()
    made = store.create_capture(
        name,
        text,
        None,
        None,
        authored_by=STUDENT,
        channel=SourceChannel.STUDENT_REPORT,
        now=AT,
        today=MONDAY,
    )
    assert isinstance(made, CaptureCreated)
    return name


def requests_in(store: HelpRequestsStore) -> list[tuple[object, ...]]:
    found = store._connection.execute("SELECT * FROM help_requests ORDER BY request_id")
    return [tuple(row) for row in found.fetchall()]


def ids_in(store: HelpRequestsStore) -> list[str]:
    found = store._connection.execute("SELECT request_id FROM help_request_ids ORDER BY 1")
    return [str(row[0]) for row in found.fetchall()]


@pytest.fixture
def path(tmp_path: pathlib.Path) -> pathlib.Path:
    return tmp_path / "record.sqlite3"


def test_a_fresh_id_makes_one_request_and_is_kept_as_used(path: pathlib.Path) -> None:
    store = HelpRequestsStore.open(path, fixture_clock())
    form = new_request_id()
    made = store.ask_once(form, PLAN_DATE, "the essay outline")

    assert isinstance(made, HelpAsked)
    assert made.request.request_id == form
    assert made.request.note == "the essay outline"
    assert [row[0] for row in requests_in(store)] == [form]
    assert ids_in(store) == [form]


@pytest.mark.parametrize("state", ["requested", "accepted", "resolved"])
def test_the_same_form_again_answers_with_the_request_as_it_stands(
    path: pathlib.Path, state: str
) -> None:
    store = HelpRequestsStore.open(path, fixture_clock())
    form = new_request_id()
    store.ask_once(form, PLAN_DATE, "  the essay outline ")
    if state == "accepted":
        store.accept(form, "After dinner")
    if state == "resolved":
        store.resolve(form, "Done together")
    before = requests_in(store)
    again = store.ask_once(form, PLAN_DATE + timedelta(days=1), "the essay outline")

    assert isinstance(again, HelpAlreadyAsked)
    assert again.request.state == state
    assert again.request.evening == PLAN_DATE
    assert requests_in(store) == before


@pytest.mark.parametrize(
    ("first", "again"),
    [
        ("  the essay outline ", "the essay outline"),
        ("line one\r\nline two", "line one\nline two"),
        ("two  spaces here", "two spaces here"),
        ("a\ttab", "a tab"),
    ],
)
def test_words_that_differ_only_in_spacing_are_the_same_form(
    path: pathlib.Path, first: str, again: str
) -> None:
    store = HelpRequestsStore.open(path, fixture_clock())
    form = new_request_id()
    store.ask_once(form, PLAN_DATE, first)

    assert isinstance(store.ask_once(form, PLAN_DATE, again), HelpAlreadyAsked)
    assert isinstance(store.ask_once(form, PLAN_DATE, again.upper()), HelpFormChanged)


@pytest.mark.parametrize("change", ["other words", "about a note", "about another note"])
def test_the_same_id_with_other_words_or_another_note_is_refused(
    path: pathlib.Path, change: str
) -> None:
    notes = practice_store(path)
    store = HelpRequestsStore.open(path, fixture_clock())
    first, second = a_note(notes), a_note(notes, "Read chapter 3")
    form = new_request_id()
    about = first if change == "about another note" else None
    store.ask_once(form, PLAN_DATE, "which part?", capture_id=about)
    before = requests_in(store)
    words = "a different question" if change == "other words" else "which part?"
    capture = {"other words": None, "about a note": first, "about another note": second}[change]
    again = store.ask_once(form, PLAN_DATE, words, capture_id=capture)

    assert isinstance(again, HelpFormChanged)
    assert again.request.request_id == form
    assert requests_in(store) == before


@pytest.mark.parametrize("gone", ["taken back", "swept", "past retention"])
def test_an_id_used_for_a_request_taken_back_or_gone_never_asks_again(
    path: pathlib.Path, gone: str
) -> None:
    clock = fixture_clock()
    store = HelpRequestsStore.open(path, clock)
    form = new_request_id()
    store.ask_once(form, PLAN_DATE, "the outline")
    if gone == "taken back":
        assert store.take_back(form)
    else:
        store.resolve(form, "Done")
        later = FrozenClock(clock.now() + timedelta(days=HELP_RETENTION_DAYS + 1), clock.zone)
        store = HelpRequestsStore(store._connection, later)
        if gone == "swept":
            store.sweep()
    again = store.ask_once(form, PLAN_DATE, "the outline")

    assert isinstance(again, HelpFormUsed)
    assert store.get(form) is None
    assert store.open_requests() == []
    assert ids_in(store) == [form]


def test_a_fresh_id_with_the_same_words_is_a_new_request(path: pathlib.Path) -> None:
    store = HelpRequestsStore.open(path, fixture_clock())
    first = store.ask_once(new_request_id(), PLAN_DATE, "the outline")
    second = store.ask_once(new_request_id(), PLAN_DATE, "the outline")

    assert isinstance(first, HelpAsked)
    assert isinstance(second, HelpAsked)
    assert len(requests_in(store)) == 2


def test_a_request_asked_without_a_form_keeps_its_id_as_used(path: pathlib.Path) -> None:
    store = HelpRequestsStore.open(path, fixture_clock())
    asked = store.ask(PLAN_DATE, "over json")
    assert store.take_back(asked.request_id)

    assert ids_in(store) == [asked.request_id]
    assert isinstance(store.ask_once(asked.request_id, PLAN_DATE, "over json"), HelpFormUsed)


@pytest.mark.parametrize(
    "value", ["", "abc", "A" * 32, "g" * 32, "0" * 31, "0" * 33, " " + "0" * 32, "0" * 31 + "\n"]
)
def test_a_request_id_is_held_to_its_shape(path: pathlib.Path, value: str) -> None:
    store = HelpRequestsStore.open(path, fixture_clock())
    with pytest.raises(NotARequestId):
        request_id_from(value)
    with pytest.raises(NotARequestId):
        store.ask_once(value, PLAN_DATE, "the outline")
    assert requests_in(store) == []
    assert ids_in(store) == []
    assert request_id_from(new_request_id())


class FailingOn(sqlite3.Connection):
    """A connection that refuses the first statement starting with ``refuse``."""

    refuse: str | None = None

    def execute(self, sql: str, parameters: object = (), /) -> sqlite3.Cursor:
        if FailingOn.refuse is not None and sql.lstrip().startswith(FailingOn.refuse):
            FailingOn.refuse = None
            msg = "disk I/O error"
            raise sqlite3.OperationalError(msg)
        return super().execute(sql, parameters)  # type: ignore[arg-type]


@pytest.mark.parametrize(
    "statement",
    [
        "INSERT OR IGNORE INTO help_request_ids",
        "INSERT INTO help_requests",
        "INSERT OR IGNORE INTO notes_named_by_requests",
    ],
)
def test_a_send_the_file_refuses_writes_nothing_and_uses_no_id(
    path: pathlib.Path, statement: str
) -> None:
    notes = practice_store(path)
    name = a_note(notes)
    connection = sqlite3.connect(path, check_same_thread=False, factory=FailingOn)
    store = HelpRequestsStore(connection, fixture_clock())
    form = new_request_id()
    FailingOn.refuse = statement
    with pytest.raises(sqlite3.OperationalError):
        store.ask_once(form, PLAN_DATE, "which part?", capture_id=name)
    FailingOn.refuse = None
    left = (requests_in(store), ids_in(store), store._connection.in_transaction)
    kept = notes._connection.execute("SELECT * FROM notes_named_by_requests").fetchall()
    again = store.ask_once(form, PLAN_DATE, "which part?", capture_id=name)

    assert left == ([], [], False)
    assert kept == []
    assert isinstance(again, HelpAsked)


def test_a_note_that_is_not_on_record_uses_no_id(path: pathlib.Path) -> None:
    practice_store(path)
    store = HelpRequestsStore.open(path, fixture_clock())
    form = new_request_id()
    with pytest.raises(UnknownCaptureReference):
        store.ask_once(form, PLAN_DATE, "which part?", capture_id=new_capture_id())

    assert (requests_in(store), ids_in(store)) == ([], [])
    assert isinstance(store.ask_once(form, PLAN_DATE, "general"), HelpAsked)


class Racing:
    """Two connections in a fixed order: the first holds its transaction open after it
    reserves the form's id until the second is about to begin its own."""

    reserved = threading.Event()
    beginning = threading.Event()
    on = False


class Reserving(sqlite3.Connection):
    def execute(self, sql: str, parameters: object = (), /) -> sqlite3.Cursor:
        cursor = super().execute(sql, parameters)  # type: ignore[arg-type]
        if Racing.on and sql.lstrip().startswith("INSERT OR IGNORE INTO help_request_ids"):
            Racing.reserved.set()
            Racing.beginning.wait(5)
        return cursor


class Beginning(sqlite3.Connection):
    def execute(self, sql: str, parameters: object = (), /) -> sqlite3.Cursor:
        if Racing.on and sql.startswith("BEGIN IMMEDIATE"):
            Racing.beginning.set()
        return super().execute(sql, parameters)  # type: ignore[arg-type]


def test_two_connections_sending_one_form_make_one_request(path: pathlib.Path) -> None:
    first = HelpRequestsStore(
        sqlite3.connect(path, check_same_thread=False, factory=Reserving), fixture_clock()
    )
    second = HelpRequestsStore(
        sqlite3.connect(path, check_same_thread=False, factory=Beginning), fixture_clock()
    )
    form = new_request_id()
    answers: dict[str, object] = {}

    def send_first() -> None:
        answers["first"] = first.ask_once(form, PLAN_DATE, "the outline")

    def send_second() -> None:
        Racing.reserved.wait(5)
        answers["second"] = second.ask_once(form, PLAN_DATE, "the outline")

    racing = [threading.Thread(target=send_first), threading.Thread(target=send_second)]
    Racing.reserved.clear()
    Racing.beginning.clear()
    Racing.on = True
    try:
        for thread in racing:
            thread.start()
        for thread in racing:
            thread.join(10)
    finally:
        Racing.on = False

    assert isinstance(answers["first"], HelpAsked)
    assert isinstance(answers["second"], HelpAlreadyAsked)
    assert len(requests_in(first)) == 1
    assert ids_in(first) == [form]


def test_the_ids_outlive_a_restart(path: pathlib.Path) -> None:
    store = HelpRequestsStore.open(path, fixture_clock())
    form = new_request_id()
    store.ask_once(form, PLAN_DATE, "the outline")
    assert store.take_back(form)
    store.close()
    reopened = HelpRequestsStore.open(path, fixture_clock())

    assert ids_in(reopened) == [form]
    assert isinstance(reopened.ask_once(form, PLAN_DATE, "the outline"), HelpFormUsed)


def test_a_file_from_before_gains_the_ids_of_its_requests_once(path: pathlib.Path) -> None:
    """The requests still in a file from before have their ids kept as used, once, with the
    requests themselves unchanged; one taken back before the upgrade left nothing to keep."""
    store = HelpRequestsStore.open(path, fixture_clock())
    kept, resolved, gone = new_request_id(), new_request_id(), new_request_id()
    for form in (kept, resolved, gone):
        store.ask_once(form, PLAN_DATE, f"asked with {form[:4]}")
    store.resolve(resolved, "Done")
    store.take_back(gone)
    store._connection.execute("DROP TABLE help_request_ids")
    store._connection.commit()
    before = requests_in(store)
    store.close()
    first = HelpRequestsStore.open(path, fixture_clock())
    once = ids_in(first)
    first.close()
    second = HelpRequestsStore.open(path, fixture_clock())

    assert once == sorted([kept, resolved])
    assert ids_in(second) == once
    assert requests_in(second) == before
    assert isinstance(second.ask_once(kept, PLAN_DATE, f"asked with {kept[:4]}"), HelpAlreadyAsked)
    assert isinstance(second.ask_once(gone, PLAN_DATE, "asked again"), HelpAsked)


def test_the_ids_table_holds_nothing_but_ids(path: pathlib.Path) -> None:
    store = HelpRequestsStore.open(path, fixture_clock())
    form = new_request_id()
    store.ask_once(form, PLAN_DATE, "private words")
    store.take_back(form)
    columns = [row[1] for row in store._connection.execute("PRAGMA table_info(help_request_ids)")]
    rows = store._connection.execute("SELECT * FROM help_request_ids").fetchall()

    assert columns == ["request_id"]
    assert [tuple(row) for row in rows] == [(form,)]
