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
import re
import sqlite3
import threading
import uuid
from collections.abc import Iterable, Mapping
from contextlib import AbstractContextManager
from dataclasses import dataclass
from datetime import date, datetime
from typing import Final

from blossom.captures import (
    ARCHIVE,
    ATTRIBUTED,
    CLARIFY,
    CREATE,
    EDIT,
    RESTORE,
    Author,
    CandidateDecision,
    Capture,
    CaptureAlreadyCreated,
    CaptureChanged,
    CaptureConflict,
    CaptureCreated,
    CaptureDetails,
    CaptureEvent,
    CaptureHistoryReading,
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
    sound_history,
)
from blossom.reconciliation import SourceChannel

logger = logging.getLogger(__name__)

CAPTURE_NAMED: Final = """
    SELECT
        capture_id, created_order, original_text, initial, text, course, title, due_date,
        kind, note, attribution, created_at_utc, created_on, updated_at_utc, updated_on,
        revision, archived, assignment_id
    FROM homework_captures
    WHERE capture_id = ?
"""
OUTSTANDING_CAPTURES: Final = """
    SELECT
        capture_id, created_order, original_text, initial, text, course, title, due_date,
        kind, note, attribution, created_at_utc, created_on, updated_at_utc, updated_on,
        revision, archived, assignment_id
    FROM homework_captures
    WHERE (archived = 0 OR archived NOT IN (0, 1)) AND assignment_id IS NULL
    ORDER BY created_order
"""
"""A row whose archived flag is neither 0 nor 1 is read with the notes that wait, where it
is named as one that cannot be read, so no damaged flag takes a note off both lists."""
ARCHIVED_CAPTURES: Final = """
    SELECT
        capture_id, created_order, original_text, initial, text, course, title, due_date,
        kind, note, attribution, created_at_utc, created_on, updated_at_utc, updated_on,
        revision, archived, assignment_id
    FROM homework_captures
    WHERE archived = 1
    ORDER BY created_order
"""
CAPTURES_NAMED: Final = """
    SELECT
        capture_id, created_order, original_text, initial, text, course, title, due_date,
        kind, note, attribution, created_at_utc, created_on, updated_at_utc, updated_on,
        revision, archived, assignment_id
    FROM homework_captures
    WHERE capture_id IN (SELECT value FROM json_each(?))
    ORDER BY created_order
"""
"""Each read is written out whole: fixed text, put together nowhere, with a name always a
bound value. A row is decoded by position, so the four name the same columns in the same
order, which a test holds them to."""
SNAPSHOT_FIELDS_ADDED: Final = ("title", "kind", "note", "assignment_id")
"""What a moment of a note's history gained with details and adding to homework."""
INSERT_CAPTURE: Final = """
    INSERT INTO homework_captures (
        capture_id, created_order, original_text, initial, text, course, title, due_date,
        kind, note, attribution, created_at_utc, created_on, updated_at_utc, updated_on,
        revision, archived, assignment_id
    ) VALUES (?, ?, ?, ?, ?, ?, NULL, ?, NULL, NULL, ?, ?, ?, ?, ?, 1, 0, NULL)
"""
UPDATE_CAPTURE: Final = """
    UPDATE homework_captures
    SET text = ?, course = ?, title = ?, due_date = ?, kind = ?, note = ?, attribution = ?,
        archived = ?, assignment_id = ?, updated_at_utc = ?, updated_on = ?, revision = ?
    WHERE capture_id = ? AND revision = ?
"""
INSERT_CAPTURE_EVENT: Final = """
    INSERT INTO capture_events (
        event_id, capture_id, operation, before, after, revision, occurred_at_utc,
        occurred_on, authored_by, decision
    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
"""
CAPTURE_EVENTS: Final = """
    SELECT event_id, capture_id, operation, before, after, revision, occurred_at_utc,
        occurred_on, authored_by, sequence, decision
    FROM capture_events
    WHERE capture_id = ? ORDER BY sequence
"""
ADD_EVENT_DECISION: Final = "ALTER TABLE capture_events ADD COLUMN decision TEXT"
"""What was chosen about homework already on record, kept with the change that added a note
to homework. A table from before this gains the column, empty in every row it had."""


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


EVENT_ID_AS_WRITTEN: Final = re.compile(r"note-event-[0-9a-f]{12}")


def new_capture_event_id() -> str:
    """A stable id for one change to a note, drawn once and never reused."""
    return f"note-event-{uuid.uuid4().hex[:12]}"


def held_text(value: object, column: str) -> str:
    """A text column as the store writes it, which is a ``str`` and nothing else. SQLite keeps
    bytes put into a text column as bytes, and ``str()`` would make ordinary words of them,
    so a row the store never wrote could be shown as hers. Nothing is coerced here."""
    if type(value) is not str:
        msg = f"{column} holds {type(value).__name__}, not text"
        raise TypeError(msg)
    return value


def held_text_or_nothing(value: object, column: str) -> str | None:
    """A text column that may hold nothing."""
    return None if value is None else held_text(value, column)


def held_count(value: object, column: str) -> int:
    """A count as the store writes it: an ``int``, from 1. Text is not read as one, since
    Python counts ``0_1`` and the file never held that. Nought and less are not read either:
    revisions, a change's place in the file, and a note's place among notes all start at 1,
    so a lower one was written by nothing here, and a place of nought would sort its note
    ahead of every real one."""
    if type(value) is not int:
        msg = f"{column} holds {type(value).__name__}, not a count"
        raise TypeError(msg)
    if value < 1:
        msg = f"{column} holds {value}, and counts start at 1"
        raise ValueError(msg)
    return value


def held_event_id(value: object) -> str:
    """A change's id in the one shape ``new_capture_event_id`` writes. A page looks a result
    up by this id, so an id of any other shape is one nothing here gave out."""
    written = held_text(value, "event_id")
    if EVENT_ID_AS_WRITTEN.fullmatch(written) is None:
        msg = "event_id is not in the shape the store writes"
        raise ValueError(msg)
    return written


def held_day(value: object, column: str) -> date:
    """A day in the one spelling the store writes. ``20260918`` reads as a day in Python and
    is no day the store wrote, and SQLite turns a number put into a text column into it."""
    written = held_text(value, column)
    day = date.fromisoformat(written)
    if day.isoformat() != written:
        msg = f"{column} holds a day in another spelling"
        raise ValueError(msg)
    return day


def held_moment(value: object, column: str) -> datetime:
    """A moment in the one spelling the store writes."""
    written = held_text(value, column)
    moment = datetime.fromisoformat(written)
    if moment.isoformat() != written:
        msg = f"{column} holds a moment in another spelling"
        raise ValueError(msg)
    return moment


def capture_from(row: tuple[object, ...]) -> Capture:
    """One note from a row of one of the four reads, by position, or ``UnreadableCapture``.
    Every column is read as the type and the spelling the store writes it in, and as nothing
    that could be made from another: no value is coerced on its way to being her words."""
    try:
        attribution = json.loads(held_text(row[10], "attribution"))
        return Capture(
            capture_id=held_text(row[0], "capture_id"),
            created_order=held_count(row[1], "created_order"),
            original_text=held_text(row[2], "original_text"),
            initial=initial_from(held_text(row[3], "initial")),
            text=held_text(row[4], "text"),
            course=held_text_or_nothing(row[5], "course"),
            title=held_text_or_nothing(row[6], "title"),
            due_date=None if row[7] is None else held_day(row[7], "due_date"),
            kind=held_text_or_nothing(row[8], "kind"),
            note=held_text_or_nothing(row[9], "note"),
            attribution={name: FieldSource(**source) for name, source in attribution.items()},
            created_at=held_moment(row[11], "created_at_utc"),
            created_on=held_day(row[12], "created_on"),
            updated_at=held_moment(row[13], "updated_at_utc"),
            updated_on=held_day(row[14], "updated_on"),
            revision=held_count(row[15], "revision"),
            archived=archived_from(row[16]),
            assignment_id=held_text_or_nothing(row[17], "assignment_id"),
        )
    except (ValueError, TypeError, AttributeError) as fault:
        raise UnreadableCapture(str(row[0])) from fault


def initial_from(raw: str) -> CaptureWords:
    """What the first save sent, from the JSON the file holds, which must be that JSON as the
    store writes it: the words as the text rule keeps them and the day in its one spelling.
    Validating alone would tidy a damaged payload and compare a later repeat with the result."""
    sent = json.loads(raw)
    words = CaptureWords.model_validate(sent)
    if sent != json.loads(words.model_dump_json()):
        msg = "the first save is not held as it is written"
        raise ValueError(msg)
    return words


def held_flag(flag: object, column: str) -> bool:
    """A yes or no as the store writes it, the integer 0 or 1. Anything else is no answer,
    and is not read as one: ``bool`` would call every damaged value a yes."""
    if type(flag) is not int or flag not in (0, 1):
        msg = f"{column} holds {flag!r}, not 0 or 1"
        raise ValueError(msg)
    return flag == 1


def archived_from(flag: object) -> bool:
    """Whether a note is put away, from the 0 or 1 the file holds."""
    return held_flag(flag, "archived")


def snapshot_from(raw: str) -> CaptureSnapshot:
    """What stood at one moment, from the JSON the file holds, which must be that JSON as the
    store writes it. Validating alone would read a day in another spelling, or a number, as
    a day, and hand the line of changes something the file never held."""
    held = json.loads(raw)
    snapshot = CaptureSnapshot.model_validate(held)
    written = json.loads(snapshot.model_dump_json())
    if isinstance(held, dict):
        # A moment written before details and adding to homework held none of these four.
        # Absent is read as nothing in each, and only as that.
        for name in SNAPSHOT_FIELDS_ADDED:
            if name not in held and written.get(name) is None:
                written.pop(name, None)
    if held != written:
        msg = "a snapshot is not held as it is written"
        raise ValueError(msg)
    return snapshot


def decision_from(raw: str) -> CandidateDecision:
    """What was chosen about homework on record, from the JSON the file holds, which must be
    that JSON as the store writes it."""
    held = json.loads(raw)
    decision = CandidateDecision.model_validate(held)
    if held != json.loads(decision.model_dump_json()):
        msg = "a decision is not held as it is written"
        raise ValueError(msg)
    return decision


def capture_event_from(row: tuple[object, ...]) -> CaptureEvent:
    """One change from a row read by ``CAPTURE_EVENTS``, or
    ``UnreadableCapture`` for the note it belongs to: a change that cannot be read makes the
    note's history unavailable, which a page says, and is never a failure of the page. Each
    column is read as the type and the spelling the store writes, as a note's row is."""
    try:
        before = held_text_or_nothing(row[3], "before")
        return CaptureEvent(
            event_id=held_event_id(row[0]),
            capture_id=held_text(row[1], "capture_id"),
            operation=held_text(row[2], "operation"),  # type: ignore[arg-type]
            before=None if before is None else snapshot_from(before),
            after=snapshot_from(held_text(row[4], "after")),
            revision=held_count(row[5], "revision"),
            occurred_at=held_moment(row[6], "occurred_at_utc"),
            occurred_on=held_day(row[7], "occurred_on"),
            authored_by=held_text(row[8], "authored_by"),  # type: ignore[arg-type]
            sequence=held_count(row[9], "sequence"),
            decision=None if row[10] is None else decision_from(held_text(row[10], "decision")),
        )
    except (ValueError, TypeError, AttributeError) as fault:
        raise UnreadableCapture(str(row[1])) from fault


def attributed(
    values: Mapping[str, object],
    standing: Capture | None,
    source: FieldSource,
) -> dict[str, FieldSource]:
    """Who supplied each optional field that holds something once ``values`` stand.

    ``values`` names every attributed field as it will stand. A field whose
    value did not change keeps the source it had, whoever makes this change;
    one that changed, or is new, takes the source of this change; one that
    holds nothing says nothing.
    """
    kept: dict[str, FieldSource] = {}
    for name in ATTRIBUTED:
        value = values.get(name)
        if value is None:
            continue
        unchanged = standing is not None and getattr(standing, name) == value
        kept[name] = standing.attribution[name] if unchanged and standing else source
    return kept


def standing_with(standing: Capture | None, words: CaptureWords) -> dict[str, object]:
    """Every attributed field once her words, class, and day are ``words``: those three
    from the form, the rest as they stand."""
    return {
        "course": words.course,
        "due_date": words.due_date,
        "title": None if standing is None else standing.title,
        "kind": None if standing is None else standing.kind,
        "note": None if standing is None else standing.note,
    }


def with_details(
    standing: Capture,
    details: CaptureDetails,
    source: FieldSource,
    *,
    assignment_id: str | None = None,
) -> Capture:
    """The note with these details standing, each changed field saying ``source`` supplied
    it and each unchanged one keeping the hand it had, and naming ``assignment_id`` when it
    is being added to homework. Built whole, so the rules a note is held to are asked of it
    before anything is written."""
    values = details.model_dump()
    return Capture.model_validate(
        {
            **standing.model_dump(),
            **values,
            "assignment_id": assignment_id or standing.assignment_id,
            "attribution": {
                name: by.model_dump() for name, by in attributed(values, standing, source).items()
            },
        }
    )


class CaptureRecords:
    """The part of the record's store that keeps her homework notes.

    Mixed into the store of the record, which supplies the connection, the
    lock, and the transaction that reserves the writer before it reads.
    """

    _connection: sqlite3.Connection
    _lock: "threading.RLock"

    def _writing(self) -> AbstractContextManager[None]:
        raise NotImplementedError

    def reading(self) -> AbstractContextManager[None]:
        """One snapshot of the file for as many reads as the block makes; the store of the
        record supplies it."""
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
                authored_by TEXT NOT NULL,
                decision TEXT
            )
            """
        )
        self._connection.execute(
            """
            CREATE INDEX IF NOT EXISTS capture_events_by_capture
            ON capture_events (capture_id, sequence)
            """
        )
        held = {
            str(row[1]) for row in self._connection.execute("PRAGMA table_info(capture_events)")
        }
        if "decision" not in held:
            self._connection.execute(ADD_EVENT_DECISION)

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

    def sound_capture_history(
        self, capture_id: str
    ) -> tuple[Capture, tuple[CaptureEvent, ...]] | None:
        """One note with its changes, read in one snapshot and found to be one sound line, or
        ``None`` for a name that is no note. A note or a change that cannot be read, or a
        line that is broken, is ``UnreadableCapture``: a page says the note is unavailable
        and says nothing a broken line would have it say.

        This is how a page reads one note to show it, whichever page it is:
        the note's own, the page that offers to ask for help about it, and the
        request itself. The note is read by ``capture``, so there is one read
        of a single note and whatever refuses it refuses this too.
        """
        name = capture_id_from(capture_id)
        with self._lock, self.reading():
            note = self.capture(name)
            if note is None:
                return None
            return note, self._validated_capture_history_locked(note).events

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
                    reading = self._validated_capture_history_locked(standing)
                    if not same:
                        return CaptureIdTaken(standing)
                    return CaptureAlreadyCreated(standing, reading.head)
                source = FieldSource(authored_by=authored_by, channel=channel)
                note = Capture(
                    capture_id=name,
                    original_text=words.text,
                    initial=words,
                    text=words.text,
                    course=words.course,
                    due_date=words.due_date,
                    attribution=attributed(standing_with(None, words), None, source),
                    created_at=now,
                    created_on=today,
                    updated_at=now,
                    updated_on=today,
                    revision=1,
                    created_order=1,
                )
                event = self._append_capture_event_locked(
                    note, CREATE, None, authored_by, (), now=now, today=today
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
                reading = self._validated_capture_history_locked(standing)
                if standing.words == words:
                    return CaptureUnchanged(standing, reading.head)
                if standing.revision != expected_revision or standing.archived:
                    return CaptureConflict(standing)
                source = FieldSource(authored_by=authored_by, channel=channel)
                note = standing.model_copy(
                    update={
                        "text": words.text,
                        "course": words.course,
                        "due_date": words.due_date,
                        "attribution": attributed(standing_with(standing, words), standing, source),
                    }
                )
                return self._change_locked(
                    standing, note, EDIT, authored_by, reading, now=now, today=today
                )
        except (sqlite3.Error, RuntimeError, ValueError) as error:
            raise CaptureNotSaved(name, error) from error

    def clarify_capture(
        self,
        capture_id: str,
        details: CaptureDetails,
        *,
        expected_revision: int,
        authored_by: Author,
        channel: SourceChannel,
        now: datetime,
        today: date,
    ) -> CaptureChanged | CaptureUnchanged | CaptureConflict:
        """Add or change a note's details: its class, title, day, kind, and a note about the
        work. Her words are not details and are never touched here.

        She may, and a parent may: whoever it is, each field that changes says
        who supplied it and through which way in, and a field that does not
        change keeps the hand it had. The same details as stand are already
        saved. Anything else must come from the revision the page showed, on a
        note that still waits: one that was put away, or is homework already,
        takes no details.
        """
        name = capture_id_from(capture_id)
        try:
            with self._lock, self._writing():
                standing = self._required_capture_locked(name)
                reading = self._validated_capture_history_locked(standing)
                if standing.details == details:
                    return CaptureUnchanged(standing, reading.head)
                if standing.revision != expected_revision or not standing.outstanding:
                    return CaptureConflict(standing)
                note = with_details(
                    standing, details, FieldSource(authored_by=authored_by, channel=channel)
                )
                return self._change_locked(
                    standing, note, CLARIFY, authored_by, reading, now=now, today=today
                )
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
                reading = self._validated_capture_history_locked(standing)
                if standing.archived == archived:
                    return CaptureUnchanged(standing, reading.head)
                if standing.revision != expected_revision:
                    return CaptureConflict(standing)
                note = standing.model_copy(update={"archived": archived})
                operation: CaptureOperation = ARCHIVE if archived else RESTORE
                return self._change_locked(
                    standing, note, operation, authored_by, reading, now=now, today=today
                )
        except (sqlite3.Error, RuntimeError, ValueError) as error:
            raise CaptureNotSaved(name, error) from error

    def _change_locked(
        self,
        standing: Capture,
        note: Capture,
        operation: CaptureOperation,
        authored_by: Author,
        reading: CaptureHistoryReading,
        *,
        now: datetime,
        today: date,
        decision: CandidateDecision | None = None,
    ) -> CaptureChanged:
        """Write one change and its event, inside the caller's transaction. ``reading`` is
        the note's line of changes as that transaction read it, which the new change must
        carry on. The update names the revision it was decided on, so it lands on that
        revision or on nothing."""
        note = note.model_copy(
            update={"revision": standing.revision + 1, "updated_at": now, "updated_on": today}
        )
        event = self._append_capture_event_locked(
            note,
            operation,
            standing,
            authored_by,
            reading.events,
            now=now,
            today=today,
            decision=decision,
        )
        written = self._connection.execute(
            UPDATE_CAPTURE,
            (
                note.text,
                note.course,
                note.title,
                None if note.due_date is None else note.due_date.isoformat(),
                note.kind,
                note.note,
                self._attribution_json(note.attribution),
                int(note.archived),
                note.assignment_id,
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
        line: tuple[CaptureEvent, ...],
        *,
        now: datetime,
        today: date,
        decision: CandidateDecision | None = None,
    ) -> CaptureEvent:
        """Append the event of one change, inside the caller's transaction, and give it the
        place the file gave it. ``line`` is the note's changes as this transaction read
        them, empty for a first save: the new change is held to the same rules as the last
        of that line, with the note as it will stand, before anything is written."""
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
            decision=decision,
        )
        sound_history(note, [*line, event])
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
                None if event.decision is None else event.decision.model_dump_json(),
            ),
        )
        return event.model_copy(update={"sequence": cursor.lastrowid})

    def _capture_locked(self, capture_id: str) -> Capture | None:
        row = self._connection.execute(CAPTURE_NAMED, (capture_id,)).fetchone()
        return None if row is None else capture_from(row)

    def _validated_capture_history_locked(self, standing: Capture) -> CaptureHistoryReading:
        """A note's line of changes, read through this store's connection inside the caller's
        transaction and found sound against the note as that transaction read it, or
        ``UnreadableCapture``. Every write to a note on record reads this before it compares
        or changes anything, so no broken line is added to, and a save that writes nothing
        names the head of a line that is whole. Nothing here mends anything."""
        rows = self._connection.execute(CAPTURE_EVENTS, (standing.capture_id,)).fetchall()
        return sound_history(standing, [capture_event_from(row) for row in rows])

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
