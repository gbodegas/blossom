"""Her homework notes in the record's file: the part of the record's store that keeps them.

The notes live in the file the assignments live in, behind the same connection
and the same lock, because the step that later makes a note into homework has
to write the assignment and the note's link to it as one thing. Until then a
note touches nothing else in the file: no assignment, no claim about a date,
no report.

Two tables. ``homework_captures`` holds each note as it stands, with the words
of its first save, everything that save sent, and for each optional field who
supplied it. ``capture_events`` holds every change with what stood before and
after, in the order the file gave them. A note's place among all notes is the
number the file gave its first event, so it never moves. A change and its
event are written in one transaction that reserves the writer before it
reads, so two devices cannot both change one revision, and a write the file
refuses keeps neither half.

A form is compared before anything is written. A first save names a new id,
or is the same save again, which returns the note as it stands now and
writes nothing, or is another save under an id already a note's, which is
refused. An edit is compared whole, the words with the class and the day, so
the same three again are already saved whatever page sent them, and anything
else from a page that is behind is refused and overwrites nothing. An archive
and a restore move a note and never touch its words.
"""

import json
import logging
import sqlite3
import threading
import uuid
from collections.abc import Iterable
from contextlib import AbstractContextManager
from dataclasses import dataclass
from datetime import date, datetime
from typing import Final

from blossom.captures import (
    ARCHIVE,
    ATTRIBUTED,
    CREATE,
    EDIT,
    RESTORE,
    Author,
    Capture,
    CaptureAlreadyCreated,
    CaptureChanged,
    CaptureConflict,
    CaptureCreated,
    CaptureEvent,
    CaptureIdTaken,
    CaptureNotSaved,
    CaptureOperation,
    CaptureSnapshot,
    CaptureUnchanged,
    CaptureWords,
    FieldSource,
    UnknownCapture,
    UnreadableCapture,
    capture_id_from,
    kept_words,
)
from blossom.reconciliation import SourceChannel

logger = logging.getLogger(__name__)

CAPTURE_COLUMNS: Final = """
    capture_id, created_order, original_text, initial, text, course, title, due_date, kind,
    note, attribution, created_at_utc, created_on, updated_at_utc, updated_on, revision,
    archived, assignment_id
"""
CAPTURE_NAMED: Final = f"SELECT {CAPTURE_COLUMNS} FROM homework_captures WHERE capture_id = ?"  # noqa: S608
OUTSTANDING_CAPTURES: Final = f"""
    SELECT {CAPTURE_COLUMNS} FROM homework_captures
    WHERE archived = 0 AND assignment_id IS NULL
    ORDER BY created_order
"""  # noqa: S608
ARCHIVED_CAPTURES: Final = f"""
    SELECT {CAPTURE_COLUMNS} FROM homework_captures
    WHERE archived = 1
    ORDER BY created_order
"""  # noqa: S608
CAPTURES_NAMED: Final = f"""
    SELECT {CAPTURE_COLUMNS} FROM homework_captures
    WHERE capture_id IN (SELECT value FROM json_each(?))
    ORDER BY created_order
"""  # noqa: S608
"""Every statement is fixed text over a fixed column list; a name is always a bound value."""
INSERT_CAPTURE: Final = """
    INSERT INTO homework_captures (
        capture_id, created_order, original_text, initial, text, course, title, due_date,
        kind, note, attribution, created_at_utc, created_on, updated_at_utc, updated_on,
        revision, archived, assignment_id
    ) VALUES (?, ?, ?, ?, ?, ?, NULL, ?, NULL, NULL, ?, ?, ?, ?, ?, 1, 0, NULL)
"""
UPDATE_CAPTURE: Final = """
    UPDATE homework_captures
    SET text = ?, course = ?, due_date = ?, attribution = ?, archived = ?,
        updated_at_utc = ?, updated_on = ?, revision = ?
    WHERE capture_id = ? AND revision = ?
"""
INSERT_CAPTURE_EVENT: Final = """
    INSERT INTO capture_events (
        event_id, capture_id, operation, before, after, revision, occurred_at_utc,
        occurred_on, authored_by
    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
"""
CAPTURE_EVENTS: Final = """
    SELECT event_id, capture_id, operation, before, after, revision, occurred_at_utc,
        occurred_on, authored_by, sequence
    FROM capture_events
    WHERE capture_id = ? ORDER BY sequence
"""


@dataclass(frozen=True)
class CaptureReadings:
    """Notes read for a page: the ones that can be read, in the order asked for, and the ids
    of any whose row cannot. A note that cannot be read is named apart and breaks no list."""

    notes: list[Capture]
    unreadable: list[str]


@dataclass(frozen=True)
class NamedCaptures:
    """Notes asked for by id, in one statement: each that can be read, by its id, and the
    ids of any that cannot. An id the record does not have is in neither."""

    notes: dict[str, Capture]
    unreadable: list[str]


def new_capture_event_id() -> str:
    """A stable id for one change to a note, drawn once and never reused."""
    return f"note-event-{uuid.uuid4().hex[:12]}"


def capture_from(row: tuple[object, ...]) -> Capture:
    """One note from a row read by ``CAPTURE_COLUMNS``, or ``UnreadableCapture``."""
    try:
        attribution = json.loads(str(row[10]))
        return Capture(
            capture_id=str(row[0]),
            created_order=int(str(row[1])),
            original_text=str(row[2]),
            initial=CaptureWords.model_validate_json(str(row[3])),
            text=str(row[4]),
            course=None if row[5] is None else str(row[5]),
            title=None if row[6] is None else str(row[6]),
            due_date=None if row[7] is None else date.fromisoformat(str(row[7])),
            kind=None if row[8] is None else str(row[8]),
            note=None if row[9] is None else str(row[9]),
            attribution={name: FieldSource(**source) for name, source in attribution.items()},
            created_at=datetime.fromisoformat(str(row[11])),
            created_on=date.fromisoformat(str(row[12])),
            updated_at=datetime.fromisoformat(str(row[13])),
            updated_on=date.fromisoformat(str(row[14])),
            revision=int(str(row[15])),
            archived=bool(row[16]),
            assignment_id=None if row[17] is None else str(row[17]),
        )
    except (ValueError, TypeError, AttributeError) as fault:
        raise UnreadableCapture(str(row[0])) from fault


def capture_event_from(row: tuple[object, ...]) -> CaptureEvent:
    """One change from a row read by ``CAPTURE_EVENTS``."""
    return CaptureEvent(
        event_id=str(row[0]),
        capture_id=str(row[1]),
        operation=str(row[2]),  # type: ignore[arg-type]
        before=None if row[3] is None else CaptureSnapshot.model_validate_json(str(row[3])),
        after=CaptureSnapshot.model_validate_json(str(row[4])),
        revision=int(str(row[5])),
        occurred_at=datetime.fromisoformat(str(row[6])),
        occurred_on=date.fromisoformat(str(row[7])),
        authored_by=str(row[8]),  # type: ignore[arg-type]
        sequence=int(str(row[9])),
    )


def attributed(
    words: CaptureWords,
    standing: Capture | None,
    source: FieldSource,
) -> dict[str, FieldSource]:
    """Who supplied each optional field that holds something once ``words`` stand.

    A field whose value did not change keeps the source it had; one that
    changed, or is new, takes the source of this change; one that holds
    nothing says nothing.
    """
    kept: dict[str, FieldSource] = {}
    for name in ATTRIBUTED:
        value = getattr(words, name)
        if value is None:
            continue
        unchanged = standing is not None and getattr(standing, name) == value
        kept[name] = standing.attribution[name] if unchanged and standing else source
    return kept


class CaptureRecords:
    """The part of the record's store that keeps her homework notes.

    Mixed into the store of the record, which supplies the connection, the
    lock, and the transaction that reserves the writer before it reads.
    """

    _connection: sqlite3.Connection
    _lock: "threading.RLock"

    def _writing(self) -> AbstractContextManager[None]:
        raise NotImplementedError

    def _create_capture_tables(self) -> None:
        """Both tables and both indexes, in the caller's transaction, so they arrive together
        or not at all: a start that is refused any one of them leaves the file as it was."""
        self._connection.execute(
            """
            CREATE TABLE IF NOT EXISTS homework_captures (
                capture_id TEXT PRIMARY KEY,
                created_order INTEGER UNIQUE NOT NULL,
                original_text TEXT NOT NULL,
                initial TEXT NOT NULL,
                text TEXT NOT NULL,
                course TEXT,
                title TEXT,
                due_date TEXT,
                kind TEXT,
                note TEXT,
                attribution TEXT NOT NULL,
                created_at_utc TEXT NOT NULL,
                created_on TEXT NOT NULL,
                updated_at_utc TEXT NOT NULL,
                updated_on TEXT NOT NULL,
                revision INTEGER NOT NULL,
                archived INTEGER NOT NULL DEFAULT 0,
                assignment_id TEXT
            )
            """
        )
        self._connection.execute(
            """
            CREATE INDEX IF NOT EXISTS homework_captures_by_created_order
            ON homework_captures (archived, created_order)
            """
        )
        self._connection.execute(
            """
            CREATE TABLE IF NOT EXISTS capture_events (
                sequence INTEGER PRIMARY KEY AUTOINCREMENT,
                event_id TEXT UNIQUE NOT NULL,
                capture_id TEXT NOT NULL,
                operation TEXT NOT NULL,
                before TEXT,
                after TEXT NOT NULL,
                revision INTEGER NOT NULL,
                occurred_at_utc TEXT NOT NULL,
                occurred_on TEXT NOT NULL,
                authored_by TEXT NOT NULL
            )
            """
        )
        self._connection.execute(
            """
            CREATE INDEX IF NOT EXISTS capture_events_by_capture
            ON capture_events (capture_id, sequence)
            """
        )

    # ------------------------------------------------------------------ reading

    def capture(self, capture_id: str) -> Capture | None:
        """One note by id, or ``None``. A row that cannot be read is ``UnreadableCapture``,
        which is not the same as no note."""
        name = capture_id_from(capture_id)
        with self._lock:
            row = self._connection.execute(CAPTURE_NAMED, (name,)).fetchone()
        return None if row is None else capture_from(row)

    def capture_history(self, capture_id: str) -> list[CaptureEvent]:
        """Every change to one note, the first save first."""
        name = capture_id_from(capture_id)
        with self._lock:
            rows = self._connection.execute(CAPTURE_EVENTS, (name,)).fetchall()
        return [capture_event_from(row) for row in rows]

    def outstanding_captures(self) -> CaptureReadings:
        """Every note still to do something about, not archived and not yet homework, the
        first saved first, in one statement."""
        return self._capture_readings(OUTSTANDING_CAPTURES)

    def archived_captures(self) -> CaptureReadings:
        """Every archived note, the first saved first, in one statement."""
        return self._capture_readings(ARCHIVED_CAPTURES)

    def captures_named(self, capture_ids: Iterable[str]) -> NamedCaptures:
        """The notes with these ids, in one statement however many are named. A name that is
        no id of a note names none and is asked for nowhere."""
        wanted = []
        for value in dict.fromkeys(capture_ids):
            try:
                wanted.append(capture_id_from(value))
            except ValueError:
                continue
        if not wanted:
            return NamedCaptures({}, [])
        with self._lock:
            rows = self._connection.execute(CAPTURES_NAMED, (json.dumps(wanted),)).fetchall()
        read = self._decoded(rows)
        return NamedCaptures({note.capture_id: note for note in read.notes}, read.unreadable)

    def _capture_readings(self, statement: str) -> CaptureReadings:
        with self._lock:
            rows = self._connection.execute(statement).fetchall()
        return self._decoded(rows)

    @staticmethod
    def _decoded(rows: list[tuple[object, ...]]) -> CaptureReadings:
        notes: list[Capture] = []
        unreadable: list[str] = []
        for row in rows:
            try:
                notes.append(capture_from(row))
            except UnreadableCapture:
                logger.warning("the homework note %s cannot be read", row[0])
                unreadable.append(str(row[0]))
        return CaptureReadings(notes, unreadable)

    # ------------------------------------------------------------------ writing

    def create_capture(
        self,
        capture_id: str,
        text: str | None,
        course: str | None,
        due_date: date | None,
        *,
        authored_by: Author,
        channel: SourceChannel,
        now: datetime,
        today: date,
    ) -> CaptureCreated | CaptureAlreadyCreated | CaptureIdTaken:
        """Save a note for the first time, once.

        The id and the words are held to their rules before anything is read.
        Then, in one transaction that reserves the writer first: an id that
        is a note's already is either this same save again, everything it
        sent being what that note's first save sent, which writes nothing and
        returns the note as it stands now, edited or archived as it may be,
        or it is another save, which is refused. Otherwise the first event
        and the note are written together.
        """
        name = capture_id_from(capture_id)
        words = kept_words(text, course, due_date)
        try:
            with self._lock, self._writing():
                standing = self._capture_locked(name)
                if standing is not None:
                    same = standing.initial == words
                    return CaptureAlreadyCreated(standing) if same else CaptureIdTaken(standing)
                source = FieldSource(authored_by=authored_by, channel=channel)
                note = Capture(
                    capture_id=name,
                    original_text=words.text,
                    initial=words,
                    text=words.text,
                    course=words.course,
                    due_date=words.due_date,
                    attribution=attributed(words, None, source),
                    created_at=now,
                    created_on=today,
                    updated_at=now,
                    updated_on=today,
                    revision=1,
                    created_order=1,
                )
                event = self._append_capture_event_locked(
                    note, CREATE, None, authored_by, now=now, today=today
                )
                assert event.sequence is not None  # noqa: S101  (the file just gave it)
                note = note.model_copy(update={"created_order": event.sequence})
                self._connection.execute(
                    INSERT_CAPTURE,
                    (
                        note.capture_id,
                        note.created_order,
                        note.original_text,
                        note.initial.model_dump_json(),
                        note.text,
                        note.course,
                        None if note.due_date is None else note.due_date.isoformat(),
                        self._attribution_json(note.attribution),
                        now.isoformat(),
                        today.isoformat(),
                        now.isoformat(),
                        today.isoformat(),
                    ),
                )
                return CaptureCreated(note, event)
        except (sqlite3.Error, RuntimeError, ValueError) as error:
            raise CaptureNotSaved(name, error) from error

    def edit_capture(
        self,
        capture_id: str,
        text: str | None,
        course: str | None,
        due_date: date | None,
        *,
        expected_revision: int,
        authored_by: Author,
        channel: SourceChannel,
        now: datetime,
        today: date,
    ) -> CaptureChanged | CaptureUnchanged | CaptureConflict:
        """Change what stands on a note: the words, the class, and the day, compared whole.

        The same three as stand now are already saved, whatever page sent
        them, and nothing is written. Anything else must come from the
        revision the page showed, and the note must not be archived; a page
        that is behind is refused with the note as it stands, so newer words,
        a newer class, or a newer day is never lost to it, and an archived
        note is never brought back by an edit. The first words are not
        touched. An optional field keeps who supplied it unless this edit
        changed it.
        """
        name = capture_id_from(capture_id)
        words = kept_words(text, course, due_date)
        try:
            with self._lock, self._writing():
                standing = self._required_capture_locked(name)
                if standing.words == words:
                    return CaptureUnchanged(standing)
                if standing.revision != expected_revision or standing.archived:
                    return CaptureConflict(standing)
                source = FieldSource(authored_by=authored_by, channel=channel)
                note = standing.model_copy(
                    update={
                        "text": words.text,
                        "course": words.course,
                        "due_date": words.due_date,
                        "attribution": attributed(words, standing, source),
                    }
                )
                return self._change_locked(standing, note, EDIT, authored_by, now=now, today=today)
        except (sqlite3.Error, RuntimeError, ValueError) as error:
            raise CaptureNotSaved(name, error) from error

    def archive_capture(
        self,
        capture_id: str,
        *,
        expected_revision: int,
        authored_by: Author,
        now: datetime,
        today: date,
    ) -> CaptureChanged | CaptureUnchanged | CaptureConflict:
        """Put a note away. It is kept, with its history, and can be brought back."""
        return self._move_capture(
            capture_id, True, expected_revision, authored_by, now=now, today=today
        )

    def restore_capture(
        self,
        capture_id: str,
        *,
        expected_revision: int,
        authored_by: Author,
        now: datetime,
        today: date,
    ) -> CaptureChanged | CaptureUnchanged | CaptureConflict:
        """Bring an archived note back to the place it had. Its words are as they were left,
        and nothing is made of it."""
        return self._move_capture(
            capture_id, False, expected_revision, authored_by, now=now, today=today
        )

    def _move_capture(
        self,
        capture_id: str,
        archived: bool,
        expected_revision: int,
        authored_by: Author,
        *,
        now: datetime,
        today: date,
    ) -> CaptureChanged | CaptureUnchanged | CaptureConflict:
        """Archive or restore. Already where it was asked to be is already done and writes
        nothing; otherwise the page's revision must be the note's. No words are touched."""
        name = capture_id_from(capture_id)
        try:
            with self._lock, self._writing():
                standing = self._required_capture_locked(name)
                if standing.archived == archived:
                    return CaptureUnchanged(standing)
                if standing.revision != expected_revision:
                    return CaptureConflict(standing)
                note = standing.model_copy(update={"archived": archived})
                operation: CaptureOperation = ARCHIVE if archived else RESTORE
                return self._change_locked(
                    standing, note, operation, authored_by, now=now, today=today
                )
        except (sqlite3.Error, RuntimeError, ValueError) as error:
            raise CaptureNotSaved(name, error) from error

    def _change_locked(
        self,
        standing: Capture,
        note: Capture,
        operation: CaptureOperation,
        authored_by: Author,
        *,
        now: datetime,
        today: date,
    ) -> CaptureChanged:
        """Write one change and its event, inside the caller's transaction. The update names
        the revision it was decided on, so it lands on that revision or on nothing."""
        note = note.model_copy(
            update={"revision": standing.revision + 1, "updated_at": now, "updated_on": today}
        )
        event = self._append_capture_event_locked(
            note, operation, standing, authored_by, now=now, today=today
        )
        written = self._connection.execute(
            UPDATE_CAPTURE,
            (
                note.text,
                note.course,
                None if note.due_date is None else note.due_date.isoformat(),
                self._attribution_json(note.attribution),
                int(note.archived),
                now.isoformat(),
                today.isoformat(),
                note.revision,
                note.capture_id,
                standing.revision,
            ),
        ).rowcount
        if written != 1:
            msg = f"note {note.capture_id!r} moved on while it was being changed"
            raise RuntimeError(msg)
        return CaptureChanged(note, event)

    def _append_capture_event_locked(
        self,
        note: Capture,
        operation: CaptureOperation,
        before: Capture | None,
        authored_by: Author,
        *,
        now: datetime,
        today: date,
    ) -> CaptureEvent:
        """Append the event of one change, inside the caller's transaction, and give it the
        place the file gave it."""
        event = CaptureEvent(
            event_id=new_capture_event_id(),
            capture_id=note.capture_id,
            operation=operation,
            before=None if before is None else CaptureSnapshot.of(before),
            after=CaptureSnapshot.of(note),
            revision=note.revision,
            occurred_at=now,
            occurred_on=today,
            authored_by=authored_by,
        )
        cursor = self._connection.execute(
            INSERT_CAPTURE_EVENT,
            (
                event.event_id,
                event.capture_id,
                event.operation,
                None if event.before is None else event.before.model_dump_json(),
                event.after.model_dump_json(),
                event.revision,
                now.isoformat(),
                today.isoformat(),
                event.authored_by,
            ),
        )
        return event.model_copy(update={"sequence": cursor.lastrowid})

    def _capture_locked(self, capture_id: str) -> Capture | None:
        row = self._connection.execute(CAPTURE_NAMED, (capture_id,)).fetchone()
        return None if row is None else capture_from(row)

    def _required_capture_locked(self, capture_id: str) -> Capture:
        note = self._capture_locked(capture_id)
        if note is None:
            raise UnknownCapture(capture_id)
        return note

    @staticmethod
    def _attribution_json(attribution: dict[str, FieldSource]) -> str:
        return json.dumps(
            {name: source.model_dump(mode="json") for name, source in sorted(attribution.items())}
        )
