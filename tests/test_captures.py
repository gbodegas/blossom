"""A homework note in the record: what a first save keeps, how a repeat is told from another
note, and what an edit, an archive, and a restore may and may not change.

The store is opened on a temporary file with a pinned clock. No page, no model, and no
assignment is involved: a note is a note.
"""

import pathlib
import re
import sqlite3
import threading
import uuid
from collections.abc import Callable
from datetime import UTC, date, datetime, timedelta

import pytest
from pydantic import ValidationError

from blossom.authored_text import TextRefused
from blossom.captures import (
    ARCHIVE,
    CREATE,
    EDIT,
    HOUSEHOLD,
    RESTORE,
    STUDENT,
    Author,
    Capture,
    CaptureAlreadyCreated,
    CaptureChanged,
    CaptureConflict,
    CaptureCreated,
    CaptureIdTaken,
    CaptureNotSaved,
    CaptureUnchanged,
    FieldSource,
    NotACaptureId,
    NoWords,
    UnknownCapture,
    UnreadableCapture,
    capture_id_from,
    new_capture_id,
)
from blossom.noticing import planning_digest, read_everything, week_from
from blossom.reconciliation import SourceChannel
from blossom.stores import captures as captures_store
from blossom.stores.project_state import ProjectStateStore
from tests.support import PRACTICE, PRACTICE_LOG, fixture_clock, practice_store

MONDAY = date(2026, 9, 14)
AT = datetime(2026, 9, 14, 21, 0, tzinfo=UTC)
WORDS = "Geometry questions 4-8, heard from a classmate"


def day(offset: int) -> date:
    return MONDAY + timedelta(days=offset)


def create(
    store: ProjectStateStore,
    capture_id: str,
    text: str = WORDS,
    course: str | None = None,
    due: date | None = None,
    *,
    on: int = 0,
    authored_by: str = STUDENT,
) -> CaptureCreated | CaptureAlreadyCreated | CaptureIdTaken:
    return store.create_capture(
        capture_id,
        text,
        course,
        due,
        authored_by=authored_by,  # type: ignore[arg-type]
        channel=SourceChannel.STUDENT_REPORT,
        now=AT + timedelta(days=on),
        today=day(on),
    )


def edit(
    store: ProjectStateStore,
    name: str,
    text: str | None,
    course: str | None,
    due: date | None,
    revision: int,
    *,
    on: int = 0,
    authored_by: Author = STUDENT,
) -> CaptureChanged | CaptureUnchanged | CaptureConflict:
    return store.edit_capture(
        name,
        text,
        course,
        due,
        expected_revision=revision,
        authored_by=authored_by,
        channel=SourceChannel.STUDENT_REPORT,
        now=AT + timedelta(days=on),
        today=day(on),
    )


def archive(
    store: ProjectStateStore, name: str, revision: int, *, on: int = 0
) -> CaptureChanged | CaptureUnchanged | CaptureConflict:
    return store.archive_capture(
        name,
        expected_revision=revision,
        authored_by=STUDENT,
        now=AT + timedelta(days=on),
        today=day(on),
    )


def restore(
    store: ProjectStateStore, name: str, revision: int, *, on: int = 0
) -> CaptureChanged | CaptureUnchanged | CaptureConflict:
    return store.restore_capture(
        name,
        expected_revision=revision,
        authored_by=STUDENT,
        now=AT + timedelta(days=on),
        today=day(on),
    )


def created(outcome: object) -> Capture:
    assert isinstance(outcome, CaptureCreated), outcome
    return outcome.capture


def changed(outcome: object) -> Capture:
    assert isinstance(outcome, CaptureChanged), outcome
    return outcome.capture


def rows(store: ProjectStateStore) -> tuple[list[tuple[object, ...]], list[tuple[object, ...]]]:
    notes = store._connection.execute("SELECT * FROM homework_captures ORDER BY capture_id")
    events = store._connection.execute("SELECT * FROM capture_events ORDER BY sequence")
    return notes.fetchall(), events.fetchall()


def schema_of(path: pathlib.Path) -> set[str]:
    connection = sqlite3.connect(path)
    try:
        return {str(row[0]) for row in connection.execute("SELECT name FROM sqlite_master")}
    finally:
        connection.close()


@pytest.fixture
def store(tmp_path: pathlib.Path) -> ProjectStateStore:
    return practice_store(tmp_path / "record.sqlite3")


# ------------------------------------------------------------------ the id


def test_a_note_is_named_by_a_uuid_written_one_way() -> None:
    made = new_capture_id()
    assert capture_id_from(made) == made
    assert uuid.UUID(made).version == 4
    for not_one in (
        "",
        "note-1",
        made.upper(),
        "{" + made + "}",
        made.replace("-", ""),
        made + " ",
    ):
        with pytest.raises(NotACaptureId):
            capture_id_from(not_one)


def test_every_read_of_a_note_names_the_same_columns_in_the_same_order() -> None:
    """A row is decoded by position, so the four reads are written out whole and must agree.
    Each is fixed text: nothing is put together when it runs, and a name is a bound value."""
    reads = (
        captures_store.CAPTURE_NAMED,
        captures_store.OUTSTANDING_CAPTURES,
        captures_store.ARCHIVED_CAPTURES,
        captures_store.CAPTURES_NAMED,
    )
    named = [
        [column.strip() for column in read.split("FROM")[0].replace("SELECT", "").split(",")]
        for read in reads
    ]
    source = pathlib.Path(captures_store.__file__).read_text(encoding="utf-8")

    assert named[0][:2] == ["capture_id", "created_order"]
    assert len(named[0]) == 18
    assert all(columns == named[0] for columns in named)
    assert "S608" not in source
    assert not re.search(r"Final = f[\"']", source)
    assert not any("{" in read for read in reads)


def test_the_store_looks_up_nothing_under_a_name_that_is_no_id(store: ProjectStateStore) -> None:
    with pytest.raises(NotACaptureId):
        create(store, "assignment-canal-essay")
    with pytest.raises(NotACaptureId):
        store.capture("../../parent")
    assert rows(store) == ([], [])


# ------------------------------------------------------------ the first save


def test_a_first_save_keeps_her_words_what_was_sent_and_who_supplied_each_field(
    tmp_path: pathlib.Path,
) -> None:
    path = tmp_path / "record.sqlite3"
    store = practice_store(path)
    name = new_capture_id()

    outcome = create(store, name, f"  {WORDS}\r\nand the diagram  ", "  Geometry ", day(4))
    store.close()
    reopened = ProjectStateStore.open(path, fixture_clock())
    note = reopened.capture(name)
    history = reopened.capture_history(name)

    assert isinstance(outcome, CaptureCreated)
    assert note == outcome.capture
    assert note is not None
    assert note.text == note.original_text == f"{WORDS}\nand the diagram"
    assert (note.course, note.due_date) == ("Geometry", day(4))
    assert note.initial.model_dump() == {
        "text": note.text,
        "course": "Geometry",
        "due_date": day(4),
    }
    assert note.attribution == {
        "course": FieldSource(authored_by=STUDENT, channel=SourceChannel.STUDENT_REPORT),
        "due_date": FieldSource(authored_by=STUDENT, channel=SourceChannel.STUDENT_REPORT),
    }
    assert (note.title, note.kind, note.note, note.assignment_id) == (None, None, None, None)
    assert (note.revision, note.archived, note.created_on) == (1, False, MONDAY)
    assert [(event.operation, event.authored_by, event.revision) for event in history] == [
        (CREATE, STUDENT, 1)
    ]
    assert history[0].before is None
    assert history[0].after.text == note.text


def test_words_alone_are_enough_and_hold_no_attribution_for_what_was_not_said(
    store: ProjectStateStore,
) -> None:
    note = created(create(store, new_capture_id()))

    assert (note.course, note.due_date, note.attribution) == (None, None, {})


def test_with_the_sign_in_off_the_household_made_the_note(store: ProjectStateStore) -> None:
    name = new_capture_id()
    note = created(create(store, name, course="Geometry", authored_by=HOUSEHOLD))

    assert note.attribution["course"].authored_by == HOUSEHOLD
    assert store.capture_history(name)[0].authored_by == HOUSEHOLD


# ------------------------------------------------- the same form, and another


def test_the_same_form_sent_again_is_the_same_note_whatever_has_happened_to_it_since(
    tmp_path: pathlib.Path,
) -> None:
    """A response lost on the way, and a retry minutes or days later: after an edit, after an
    archive, and after a restart the repeat returns the note as it stands, restores no old
    words, and brings nothing back from the archive."""
    path = tmp_path / "record.sqlite3"
    store = practice_store(path)
    name = new_capture_id()
    first = created(create(store, name, WORDS, "Geometry", day(4)))

    again = create(store, name, WORDS, "Geometry", day(4))
    edited = changed(edit(store, name, "Questions 4-9", "Geometry", None, 1, on=1))
    after_edit = create(store, name, WORDS, "Geometry", day(4), on=1)
    archived = changed(archive(store, name, 2, on=2))
    after_archive = create(store, name, WORDS, "Geometry", day(4), on=2)
    store.close()
    reopened = ProjectStateStore.open(path, fixture_clock())
    after_restart = create(reopened, name, f" {WORDS} ", " Geometry", day(4), on=3)

    assert isinstance(again, CaptureAlreadyCreated)
    assert again.capture == first
    assert isinstance(after_edit, CaptureAlreadyCreated)
    assert after_edit.capture == edited
    assert after_edit.capture.text == "Questions 4-9"
    for outcome in (after_archive, after_restart):
        assert isinstance(outcome, CaptureAlreadyCreated)
        assert outcome.capture == archived
        assert outcome.capture.archived
        assert outcome.capture.text == "Questions 4-9"
    notes, events = rows(reopened)
    assert len(notes) == 1
    assert [row[3] for row in events] == [CREATE, EDIT, ARCHIVE]


@pytest.mark.parametrize(
    "other",
    [
        {"text": "Questions 4-9"},
        {"course": "Algebra"},
        {"course": None},
        {"due": date(2026, 9, 19)},
        {"due": None},
    ],
    ids=["other words", "another class", "no class", "another day", "no day"],
)
def test_the_same_id_with_anything_else_in_it_is_not_that_note(
    store: ProjectStateStore, other: dict[str, object]
) -> None:
    """The words alone would not tell two attempts apart: the class and the day sent with
    them are part of what the first save sent."""
    name = new_capture_id()
    sent = {"text": WORDS, "course": "Geometry", "due": day(4)}
    first = created(create(store, name, **sent))  # type: ignore[arg-type]
    before = rows(store)

    taken = create(store, name, **{**sent, **other})  # type: ignore[arg-type]
    changed(edit(store, name, "New words", None, None, 1, on=1))
    changed(archive(store, name, 2, on=1))
    still_taken = create(store, name, **{**sent, **other})  # type: ignore[arg-type]

    assert isinstance(taken, CaptureIdTaken)
    assert taken.capture == first
    assert isinstance(still_taken, CaptureIdTaken)
    assert still_taken.capture.archived
    assert len(rows(store)[0]) == len(before[0]) == 1


def test_a_save_that_writes_nothing_names_the_change_it_found_standing(
    store: ProjectStateStore,
) -> None:
    """A page says what a save did from the change it names. One that wrote nothing names
    the latest change of the note, read in the transaction that decided nothing was to do."""
    name = new_capture_id()
    first = create(store, name)
    assert isinstance(first, CaptureCreated)
    again = create(store, name)
    edited = edit(store, name, "Questions 4-9", None, None, 1, on=1)
    assert isinstance(edited, CaptureChanged)
    after_edit = create(store, name, on=2)
    same_words = edit(store, name, "Questions 4-9", None, None, 1, on=2)
    put_away = archive(store, name, 2, on=3)
    assert isinstance(put_away, CaptureChanged)
    put_away_again = archive(store, name, 2, on=4)
    after_archive = create(store, name, on=4)
    back = restore(store, name, 3, on=5)
    assert isinstance(back, CaptureChanged)
    back_again = restore(store, name, 3, on=6)

    assert isinstance(again, CaptureAlreadyCreated)
    assert again.head == first.event
    assert isinstance(after_edit, CaptureAlreadyCreated)
    assert after_edit.head == edited.event
    assert isinstance(same_words, CaptureUnchanged)
    assert same_words.head == edited.event
    assert isinstance(put_away_again, CaptureUnchanged)
    assert put_away_again.head == put_away.event
    assert isinstance(after_archive, CaptureAlreadyCreated)
    assert after_archive.head == put_away.event
    assert isinstance(back_again, CaptureUnchanged)
    assert back_again.head == back.event
    assert [event.event_id for event in store.capture_history(name)] == [
        first.event.event_id,
        edited.event.event_id,
        put_away.event.event_id,
        back.event.event_id,
    ]


@pytest.mark.parametrize(
    "damage",
    [
        "UPDATE capture_events SET occurred_on = 'someday' WHERE revision = 2",
        "UPDATE capture_events SET occurred_at_utc = 'then' WHERE revision = 2",
        "UPDATE capture_events SET after = '{' WHERE revision = 2",
        "UPDATE capture_events SET before = 'not json' WHERE revision = 2",
        "UPDATE capture_events SET operation = 'guess' WHERE revision = 2",
        "UPDATE capture_events SET authored_by = 'nobody' WHERE revision = 2",
    ],
)
def test_a_change_that_cannot_be_read_is_an_unreadable_note_and_never_a_crash(
    store: ProjectStateStore, damage: str
) -> None:
    name = new_capture_id()
    created(create(store, name))
    changed(edit(store, name, "Questions 4-9", None, None, 1))
    store._connection.execute(damage)
    store._connection.commit()

    with pytest.raises(UnreadableCapture):
        store.capture_history(name)
    with pytest.raises(CaptureNotSaved):
        edit(store, name, "Questions 4-9", None, None, 2)
    assert store.capture(name) is not None
    assert [note.capture_id for note in store.outstanding_captures().notes] == [name]


DAMAGE = {
    "head that is no json": "UPDATE capture_events SET after = '{' WHERE revision = 2",
    "earlier that is no json": "UPDATE capture_events SET after = '{' WHERE revision = 1",
    "earlier of no kind": "UPDATE capture_events SET operation = 'guess' WHERE revision = 1",
    "earlier on no day": "UPDATE capture_events SET occurred_on = 'someday' WHERE revision = 1",
    "no first save": "DELETE FROM capture_events WHERE revision = 1",
    "a revision skipped": "UPDATE capture_events SET revision = 12 WHERE revision = 2",
    "a revision twice": "UPDATE capture_events SET revision = 1 WHERE revision = 2",
    "a before that never stood": (
        "UPDATE capture_events SET before = json_set(before, '$.text', 'Never stood here') "
        "WHERE revision = 2"
    ),
    "a first save that began archived": (
        "UPDATE capture_events SET after = json_set(after, '$.archived', json('true')) "
        "WHERE revision = 1"
    ),
    "words too long to have been kept": (
        "UPDATE capture_events SET after = json_set(after, '$.course', "
        "'cccccccccccccccccccccccccccccccccccccccccccccccccccccccccccccccccccc') WHERE revision = 1"
    ),
    "a note its changes do not end at": (
        "UPDATE homework_captures SET text = 'Words no change made'"
    ),
    "a revision its changes do not end at": "UPDATE homework_captures SET revision = 3",
    "a first save that sent something else": (
        "UPDATE homework_captures SET initial = json_set(initial, '$.text', 'Other first words'), "
        "original_text = 'Other first words'"
    ),
    "a place the file did not give": "UPDATE homework_captures SET created_order = 99",
}


@pytest.mark.parametrize(
    "action", ["the same form again", "the same words", "other words", "archive"]
)
@pytest.mark.parametrize("damage", sorted(DAMAGE))
def test_a_line_of_changes_that_is_not_sound_is_neither_added_to_nor_said_to_stand(
    store: ProjectStateStore, damage: str, action: str
) -> None:
    """Whatever is asked of a note, its changes are read first, inside the transaction, and
    must be one line: a first save at revision 1, each change starting where the one before
    ended, each of its own kind, within the text rules, ending at the note as it stands. A
    line that is not is refused with its cause, nothing is written, and nothing is mended."""
    name = new_capture_id()
    created(create(store, name))
    changed(edit(store, name, "Questions 4-9", None, None, 1, on=1))
    store._connection.execute(DAMAGE[damage])
    store._connection.commit()
    before = rows(store)

    attempts: dict[str, Callable[[], object]] = {
        "the same form again": lambda: create(store, name, on=2),
        "the same words": lambda: edit(store, name, "Questions 4-9", None, None, 2, on=2),
        "other words": lambda: edit(store, name, "Questions 4-10", None, None, 2, on=2),
        "archive": lambda: archive(store, name, 2, on=2),
    }
    with pytest.raises(CaptureNotSaved) as refused:
        attempts[action]()

    assert isinstance(refused.value.__cause__, UnreadableCapture)
    assert rows(store) == before
    assert not store._connection.in_transaction
    with pytest.raises(UnreadableCapture):
        store.sound_capture_history(name)


@pytest.mark.parametrize(
    "damage",
    [
        "UPDATE capture_events SET operation = 'edit' WHERE operation = 'restore'",
        "UPDATE capture_events SET after = json_set(after, '$.text', 'Slipped in') "
        "WHERE operation = 'archive'",
        "UPDATE capture_events SET operation = 'restore' WHERE operation = 'archive'",
    ],
)
def test_a_change_that_is_not_what_its_kind_does_makes_the_line_unsound(
    store: ProjectStateStore, damage: str
) -> None:
    """An archive and a restore move a note one way each and touch no words, and an edit
    never brings an archived note back."""
    name = new_capture_id()
    created(create(store, name))
    changed(archive(store, name, 1))
    changed(restore(store, name, 2))
    store._connection.execute(damage)
    store._connection.commit()
    before = rows(store)

    attempts: tuple[Callable[[], object], ...] = (
        lambda: restore(store, name, 3),
        lambda: archive(store, name, 3),
        lambda: edit(store, name, "Other words", None, None, 3),
    )
    for attempt in attempts:
        with pytest.raises(CaptureNotSaved):
            attempt()
    assert rows(store) == before


def test_a_sound_line_is_added_to_whatever_the_clock_does_and_a_save_that_writes_nothing_works(
    store: ProjectStateStore,
) -> None:
    """Days that run backward are no fault of a line, and nothing here asks them to go on."""
    name = new_capture_id()
    first = create(store, name, course="Geometry", due=day(9), on=6)
    assert isinstance(first, CaptureCreated)
    second = changed(edit(store, name, "Questions 4-9", "Geometry", None, 1, on=4))
    third = changed(archive(store, name, 2, on=2))
    again = archive(store, name, 3, on=1)
    fourth = changed(restore(store, name, 3, on=0))
    replay = create(store, name, course="Geometry", due=day(9), on=0)
    fifth = changed(edit(store, name, "Questions 4-10", None, None, 4, on=0))
    reading = store.sound_capture_history(name)

    assert (second.revision, third.revision, fourth.revision, fifth.revision) == (2, 3, 4, 5)
    assert isinstance(again, CaptureUnchanged)
    assert again.head.operation == ARCHIVE
    assert isinstance(replay, CaptureAlreadyCreated)
    assert replay.head.operation == RESTORE
    assert reading is not None
    note, changes = reading
    assert note == fifth
    assert [change.operation for change in changes] == [CREATE, EDIT, ARCHIVE, RESTORE, EDIT]
    assert [change.occurred_on for change in changes] == [day(6), day(4), day(2), day(0), day(0)]


def test_a_name_that_is_no_note_has_no_line_of_changes(store: ProjectStateStore) -> None:
    assert store.sound_capture_history(new_capture_id()) is None


def test_the_store_of_the_record_says_how_long_it_keeps_her_notes_and_why() -> None:
    """Its policy is prose that states the schedule and the reason, and a note is on another
    schedule than the assignments the store also keeps: nothing takes one away."""
    policy = ProjectStateStore.retention_policy
    guide = (pathlib.Path(__file__).parents[1] / "docs" / "development.md").read_text(
        encoding="utf-8"
    )

    assert "academic year" in policy
    assert "homework notes" in policy
    for said in ("every change", "putting one away keeps it", "nothing sweeps them", "backup"):
        assert said in policy
        assert said in " ".join(guide.split())


def test_two_notes_with_the_same_words_are_two_notes(store: ProjectStateStore) -> None:
    first = created(create(store, new_capture_id()))
    second = created(create(store, new_capture_id()))

    assert first.capture_id != second.capture_id
    assert [note.capture_id for note in store.outstanding_captures().notes] == [
        first.capture_id,
        second.capture_id,
    ]


# ------------------------------------------------------------------- an edit


def test_an_edit_changes_what_stands_and_never_the_first_words(store: ProjectStateStore) -> None:
    name = new_capture_id()
    created(create(store, name, WORDS, "Geometry", day(4)))

    note = changed(
        edit(store, name, "Questions 4-9", "Geometry", day(5), 1, on=1, authored_by=HOUSEHOLD)
    )
    event = store.capture_history(name)[-1]

    assert (note.text, note.original_text, note.initial.text) == ("Questions 4-9", WORDS, WORDS)
    assert (note.revision, note.updated_on, note.created_on) == (2, day(1), MONDAY)
    assert note.attribution["course"].authored_by == STUDENT
    assert note.attribution["due_date"].authored_by == HOUSEHOLD
    assert (event.operation, event.revision, event.authored_by) == (EDIT, 2, HOUSEHOLD)
    assert event.before is not None
    assert (event.before.text, event.before.due_date) == (WORDS, day(4))
    assert (event.after.text, event.after.due_date) == ("Questions 4-9", day(5))


def test_a_field_she_clears_holds_nothing_and_says_nothing_of_who(store: ProjectStateStore) -> None:
    name = new_capture_id()
    created(create(store, name, WORDS, "Geometry", day(4)))

    note = changed(edit(store, name, WORDS, "  ", None, 1, on=1))

    assert (note.course, note.due_date, note.attribution) == (None, None, {})


def test_an_edit_is_compared_whole_and_a_page_that_is_behind_overwrites_nothing(
    store: ProjectStateStore,
) -> None:
    """What stands is the words, the class, and the day together. The same three again are
    already saved whatever page sent them; anything else from a page that is behind is
    refused, so a newer class or day is never lost to old words."""
    name = new_capture_id()
    created(create(store, name, WORDS, None, None))
    newer = changed(edit(store, name, WORDS, "Geometry", day(4), 1, on=1))
    before = rows(store)

    same = edit(store, name, WORDS, "Geometry", day(4), 1, on=2)
    behind = edit(store, name, "Old page words", None, None, 1, on=2)
    text_only = edit(store, name, WORDS, None, None, 1, on=2)

    assert isinstance(same, CaptureUnchanged)
    assert same.capture == newer
    for refused in (behind, text_only):
        assert isinstance(refused, CaptureConflict)
        assert refused.capture == newer
    assert rows(store) == before


# ------------------------------------------------------- archive and restore


def test_archive_and_restore_move_a_note_and_change_none_of_its_words(
    store: ProjectStateStore,
) -> None:
    name = new_capture_id()
    created(create(store, name, WORDS, "Geometry", day(4)))
    changed(edit(store, name, "Questions 4-9", "Geometry", day(4), 1, on=1))

    archived = changed(archive(store, name, 2, on=2))
    again = archive(store, name, 2, on=2)
    restored = changed(restore(store, name, 3, on=3))
    again_restored = restore(store, name, 3, on=3)

    assert (archived.archived, archived.revision, archived.text) == (True, 3, "Questions 4-9")
    assert isinstance(again, CaptureUnchanged)
    assert (restored.archived, restored.revision, restored.text) == (False, 4, "Questions 4-9")
    assert restored.assignment_id is None
    assert isinstance(again_restored, CaptureUnchanged)
    assert [event.operation for event in store.capture_history(name)] == [
        CREATE,
        EDIT,
        ARCHIVE,
        RESTORE,
    ]
    assert {item.assignment_id for item in store.all_assignments()} == {PRACTICE, PRACTICE_LOG}


def test_a_page_that_is_behind_neither_archives_restores_nor_edits_an_archived_note(
    store: ProjectStateStore,
) -> None:
    name = new_capture_id()
    created(create(store, name))
    changed(edit(store, name, "Newer words", None, None, 1, on=1))

    stale_archive = archive(store, name, 1, on=1)
    changed(archive(store, name, 2, on=1))
    before = rows(store)
    stale_edit = edit(store, name, "From before", None, None, 2, on=2)
    current_edit = edit(store, name, "While archived", None, None, 3, on=2)
    stale_restore = restore(store, name, 2, on=2)

    for refused in (stale_archive, stale_edit, current_edit, stale_restore):
        assert isinstance(refused, CaptureConflict)
    assert rows(store) == before
    note = store.capture(name)
    assert note is not None
    assert (note.archived, note.text) == (True, "Newer words")


def test_the_oldest_note_stays_first_whatever_is_done_to_it(store: ProjectStateStore) -> None:
    """The list is in the order notes were first saved. An edit does not send a note to the
    back, and one brought back from the archive returns to the place it had."""
    names = [new_capture_id() for _ in range(4)]
    for index, name in enumerate(names):
        created(create(store, name, f"note {index}", on=index))
    changed(edit(store, names[0], "note 0, edited last", None, None, 1, on=9))
    changed(archive(store, names[1], 1, on=9))
    store._connection.execute(
        "UPDATE homework_captures SET assignment_id = ? WHERE capture_id = ?", (PRACTICE, names[2])
    )
    store._connection.commit()

    waiting = store.outstanding_captures()
    put_away = store.archived_captures()
    changed(restore(store, names[1], 2, on=10))

    assert [note.capture_id for note in waiting.notes] == [names[0], names[3]]
    assert [note.capture_id for note in put_away.notes] == [names[1]]
    assert [note.capture_id for note in store.outstanding_captures().notes] == [
        names[0],
        names[1],
        names[3],
    ]
    assert (waiting.unreadable, put_away.unreadable) == ([], [])


def test_a_change_names_a_note_on_record(store: ProjectStateStore) -> None:
    nobody = new_capture_id()
    with pytest.raises(UnknownCapture):
        edit(store, nobody, WORDS, None, None, 1, on=0)
    with pytest.raises(UnknownCapture):
        archive(store, nobody, 1, on=0)
    with pytest.raises(UnknownCapture):
        restore(store, nobody, 1, on=0)
    assert store.capture(nobody) is None
    assert store.capture_history(nobody) == []


# ----------------------------------------------------------- the words kept


@pytest.mark.parametrize(
    ("text", "course", "refusal"),
    [
        ("", None, NoWords),
        (" \r\n ", None, NoWords),
        ("x" * 501, None, TextRefused),
        ("a bell" + chr(7), None, TextRefused),
        ("half" + chr(0xD800), None, TextRefused),
        (WORDS, "c" * 61, TextRefused),
        (WORDS, "Geo" + chr(10) + "metry", TextRefused),
        (WORDS, "Geo" + chr(9) + "metry", TextRefused),
        (WORDS, "Geo" + chr(0x2028) + "metry", TextRefused),
    ],
    ids=["blank", "only space", "501", "control", "surrogate", "course 61", "newline", "tab", "LS"],
)
def test_words_the_record_will_not_keep_are_refused_whole_and_nothing_is_written(
    store: ProjectStateStore, text: str, course: str | None, refusal: type[Exception]
) -> None:
    name = new_capture_id()
    with pytest.raises(refusal):
        create(store, name, text, course)
    good = created(create(store, name))
    with pytest.raises(refusal):
        edit(store, name, text, course, None, 1, on=1)

    assert store.capture(name) == good
    assert len(rows(store)[1]) == 1


def test_words_at_the_limit_with_joined_characters_and_a_tab_are_kept_as_written(
    store: ProjectStateStore,
) -> None:
    family = chr(0x1F469) + chr(0x200D) + chr(0x1F52C)
    text = ("x" * 490) + chr(9) + family + chr(0xFFFD) + "\nlast"
    note = created(create(store, new_capture_id(), text, "c" * 60))

    assert len(text) == 500
    assert (note.text, note.course) == (text, "c" * 60)


def test_the_model_refuses_what_the_store_would_not_have_written() -> None:
    whole = {
        "capture_id": new_capture_id(),
        "original_text": WORDS,
        "initial": {"text": WORDS},
        "text": WORDS,
        "created_at": AT,
        "created_on": MONDAY,
        "updated_at": AT,
        "updated_on": MONDAY,
        "revision": 1,
        "created_order": 1,
    }
    assert Capture(**whole).outstanding  # type: ignore[arg-type]
    for broken in (
        {"capture_id": "note-1"},
        {"original_text": "other first words"},
        {"text": "x" * 501},
        {"course": "Geometry"},
        {"revision": 0},
    ):
        with pytest.raises(ValidationError):
            Capture(**{**whole, **broken})  # type: ignore[arg-type]


# ------------------------------------------------------------ two connections


def test_two_devices_saving_one_form_make_one_note(tmp_path: pathlib.Path) -> None:
    path = tmp_path / "record.sqlite3"
    first = practice_store(path)
    second = ProjectStateStore.open(path, fixture_clock())
    name = new_capture_id()
    outcomes: list[object] = []
    ready = threading.Barrier(2)

    def save(store: ProjectStateStore) -> None:
        ready.wait(timeout=10)
        outcomes.append(create(store, name, WORDS, "Geometry"))

    threads = [threading.Thread(target=save, args=(store,)) for store in (first, second)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=30)

    assert sorted(type(outcome).__name__ for outcome in outcomes) == [
        "CaptureAlreadyCreated",
        "CaptureCreated",
    ]
    assert len(rows(first)[0]) == 1
    assert len(rows(first)[1]) == 1


def test_two_devices_changing_one_note_from_the_same_page_change_it_once(
    tmp_path: pathlib.Path,
) -> None:
    """One edits and one archives, both from revision 1, through two connections to the
    file: one change is made and the other finds the note moved on."""
    path = tmp_path / "record.sqlite3"
    first = practice_store(path)
    second = ProjectStateStore.open(path, fixture_clock())
    name = new_capture_id()
    created(create(first, name))
    outcomes: list[object] = []
    ready = threading.Barrier(2)

    def one_edits() -> None:
        ready.wait(timeout=10)
        outcomes.append(edit(first, name, "Edited", None, None, 1, on=1))

    def other_archives() -> None:
        ready.wait(timeout=10)
        outcomes.append(archive(second, name, 1, on=1))

    threads = [threading.Thread(target=one_edits), threading.Thread(target=other_archives)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=30)

    assert sorted(type(outcome).__name__ for outcome in outcomes) == [
        "CaptureChanged",
        "CaptureConflict",
    ]
    note = first.capture(name)
    assert note is not None
    assert note.revision == 2
    assert len(rows(first)[1]) == 2


# ------------------------------------------------------ the file, and failure


def test_both_tables_and_their_indexes_arrive_together_or_not_at_all(
    tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A start that is refused the last index leaves a file from before exactly as it was,
    and the next start makes all four and keeps every row that was there."""
    path = tmp_path / "record.sqlite3"
    before = practice_store(path)
    before._connection.execute("DROP TABLE capture_events")
    before._connection.execute("DROP TABLE homework_captures")
    before._connection.commit()
    before.close()
    made = {
        "homework_captures",
        "homework_captures_by_created_order",
        "capture_events",
        "capture_events_by_capture",
    }
    assert not made & schema_of(path)
    real_connect = sqlite3.connect
    opened: list[sqlite3.Connection] = []

    def no_last_index(action: int, first: str | None, *rest: object) -> int:
        refused = action == sqlite3.SQLITE_CREATE_INDEX and first == "capture_events_by_capture"
        return sqlite3.SQLITE_DENY if refused else sqlite3.SQLITE_OK

    def connect(*args: object, **kwargs: object) -> sqlite3.Connection:
        connection = real_connect(*args, **kwargs)  # type: ignore[call-overload]
        connection.set_authorizer(no_last_index)
        opened.append(connection)
        return connection  # type: ignore[no-any-return]

    monkeypatch.setattr(sqlite3, "connect", connect)
    with pytest.raises(sqlite3.DatabaseError):
        ProjectStateStore.open(path, fixture_clock())
    for connection in opened:
        connection.close()
    monkeypatch.undo()

    assert not made & schema_of(path)
    store = ProjectStateStore.open(path, fixture_clock())
    assert made <= schema_of(path)
    assert {item.assignment_id for item in store.all_assignments()} == {PRACTICE, PRACTICE_LOG}
    created(create(store, new_capture_id()))
    store.close()
    again = ProjectStateStore.open(path, fixture_clock())
    assert len(again.outstanding_captures().notes) == 1


def test_a_write_the_file_refuses_keeps_neither_the_note_nor_its_event(
    store: ProjectStateStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    name = new_capture_id()
    other = new_capture_id()
    created(create(store, name))
    before = rows(store)

    def refuses(*args: object, **kwargs: object) -> None:
        msg = "the file refused"
        raise sqlite3.OperationalError(msg)

    monkeypatch.setattr(store, "_append_capture_event_locked", refuses)
    with pytest.raises(CaptureNotSaved):
        create(store, other)
    with pytest.raises(CaptureNotSaved):
        edit(store, name, "Edited", None, None, 1, on=1)
    with pytest.raises(CaptureNotSaved):
        archive(store, name, 1, on=1)
    monkeypatch.undo()

    assert rows(store) == before
    assert store.capture(other) is None
    assert not store._connection.in_transaction
    changed(edit(store, name, "Edited", None, None, 1, on=1))


def test_a_row_that_cannot_be_read_is_named_apart_and_breaks_no_list(
    store: ProjectStateStore,
) -> None:
    good = created(create(store, new_capture_id(), "a good note"))
    bad = created(create(store, new_capture_id(), "a note about to be damaged", on=1))
    store._connection.execute(
        "UPDATE homework_captures SET due_date = 'next week' WHERE capture_id = ?",
        (bad.capture_id,),
    )
    store._connection.commit()

    waiting = store.outstanding_captures()
    named = store.captures_named([good.capture_id, bad.capture_id, new_capture_id()])

    assert [note.capture_id for note in waiting.notes] == [good.capture_id]
    assert waiting.unreadable == [bad.capture_id]
    assert list(named.notes) == [good.capture_id]
    assert named.unreadable == [bad.capture_id]
    with pytest.raises(UnreadableCapture):
        store.capture(bad.capture_id)
    with pytest.raises(CaptureNotSaved):
        archive(store, bad.capture_id, 1, on=1)


# ------------------------------------------------------- what a note is not


def test_a_note_is_no_assignment_and_changes_nothing_a_plan_is_made_from(
    store: ProjectStateStore,
) -> None:
    before = planning_digest(week_from(read_everything(store, store), MONDAY))
    on_record = [item.model_dump() for item in store.all_assignments()]
    name = new_capture_id()

    created(create(store, name, "Weekly practice, all of it", "Math", day(2)))
    changed(edit(store, name, "Weekly practice", "Math", day(3), 1, on=1))
    changed(archive(store, name, 2, on=1))
    changed(restore(store, name, 3, on=1))

    assert planning_digest(week_from(read_everything(store, store), MONDAY)) == before
    assert [item.model_dump() for item in store.all_assignments()] == on_record
    assert store._connection.execute("SELECT COUNT(*) FROM date_claims").fetchone()[0] == 0


@pytest.mark.parametrize("count", [1, 20, 200])
def test_each_list_of_notes_is_one_statement_however_many_there_are(
    count: int, tmp_path: pathlib.Path
) -> None:
    store = practice_store(tmp_path / "record.sqlite3")
    names = [new_capture_id() for _ in range(count)]
    for index, name in enumerate(names):
        created(create(store, name, f"note {index}"))
    for name in names[::2]:
        changed(archive(store, name, 1, on=1))
    seen: list[str] = []
    store._connection.set_trace_callback(seen.append)

    waiting = store.outstanding_captures()
    put_away = store.archived_captures()
    named = store.captures_named(names)
    store._connection.set_trace_callback(None)

    assert len(waiting.notes) + len(put_away.notes) == count
    assert len(named.notes) == count
    assert len(seen) == 3
