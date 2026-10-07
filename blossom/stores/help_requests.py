# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Gerardo Bodegas Martinez
"""Her request for help, and what a parent did with it, kept where she can see both.

Asking for help is a gesture like the "too much" signal: one press, with a
sentence if she wants one and no form to fill. Unlike the signal, it is
addressed to a person, so it has a state a person moves: requested, until a
parent takes it up; accepted, while the parent is on it; resolved, once a
parent closes it. Each step shows on her page in plain words, and nothing
says a parent is looking into something before that parent has said so. She
can take a request back while nobody has taken it up.

A parent's words go on the request as updates, in the order they came: words
sent with I can help, each update added while the request is taken up, and
any final words sent with the close. They are kept as the family page's box
keeps them, with one kind of line ending and the edges trimmed, and the cap
counts them that way, whichever route sends them. Each is an append in one
statement, so two sent at once both stay, and a closed request takes none.
A move reads the clock only once it holds the writer, so the updates' times
follow the order they are kept in. Each of a parent's forms carries an id of
its own, kept with the update it added: the same form sent again with the
same words finds that update and writes nothing, and a second I can help
adds no words at all, so no message is added twice. The latest update's
words are kept in the reply's own column as well, so a build that reads only
that column still shows the latest. A reply that no update holds becomes the
request's latest update at the next start, with no time, since none was
kept: a reply kept from before updates, or words that a build reading only
that column saved after the last start.

The store keeps a resolved request for two weeks, long enough for a parent's
updates to be read, and applies that cutoff on every read as well as in the
sweep; the updates are in the request's row, so they go with it. An open
request is kept until someone resolves it: a request is a question to a
person, and a question nobody has answered is not old news. Nothing here
counts requests or groups them by anything; a record like that would be
about her rather than about the help.

A request can be about one of her homework notes. It then carries the note's
id and nothing of its words: the note is shown beside the request from the
record as it stands when the page is read. The notes are in this same file,
kept by the record's store through a connection of its own, so the name is
checked here, on this store's connection, inside the transaction that writes
the request and begun before the note is looked for. Nothing is written
through two connections at once, and a name that is no note of this record
writes nothing. Putting the note away later does not remove the reference,
and a note that cannot be read later does not take the request with it.

Each form that asks carries an id of its own, and the request is kept under
it. The same form sent again makes no second request, and an id once used
never makes another, even after its request is taken back or gone: only the
id is kept for that, and nothing else of the request.
"""

import json
import logging
import re
import sqlite3
import threading
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Final, Literal, cast
from uuid import uuid4

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field

from blossom.captures import capture_id_from
from blossom.clock import Clock
from blossom.stores.paths import refuse_unsafe_path
from blossom.stores.project_state import HeldText, normalize_note
from blossom.unreadable import refusal_in_names, text_or_refusal

logger = logging.getLogger(__name__)

HELP_RETENTION_DAYS: Final = 14
"""How long a resolved request is kept: long enough for a parent's updates to be read."""

HELP_RECENT_DAYS: Final = 7
"""How long a resolved request stays among her help updates, counted from when it was
resolved. After that it is kept apart, with those resolved earlier, until retention
takes it."""

NOTE_MAX_LENGTH: Final = 500
"""The most that is kept of her note or of a parent's update: a sentence or two,
the same cap as everywhere else words are typed into this application."""

HelpState = Literal["requested", "accepted", "resolved"]
"""Where a request stands: asked and not yet taken up, taken up by a parent, or
answered. Only a parent moves it forward; only she takes it back, and only
while it is still just requested."""


class ParentUpdate(BaseModel):
    """One message a parent added to a request: the id of the form that added it, its words,
    and when, by the real clock, or no time for a reply kept from before updates."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    update_id: str
    body: str = Field(min_length=1, max_length=NOTE_MAX_LENGTH)
    written_at: AwareDatetime | None = None


class HelpRequest(BaseModel):
    """One request for help: when, about which evening, her words, and where it stands."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    request_id: str
    evening: date
    """The household date she asked on, by the household's clock."""
    asked_at: AwareDatetime
    """When she asked, by the real clock, so retention runs even when the clock is pinned."""
    note: str | None = Field(default=None, max_length=NOTE_MAX_LENGTH)
    """What she said she is stuck on, if she said. Never required."""
    state: HelpState = "requested"
    accepted_at: AwareDatetime | None = None
    resolved_at: AwareDatetime | None = None
    response: str | None = Field(default=None, max_length=NOTE_MAX_LENGTH)
    """The latest update's words, kept in the reply's own column for a build that reads only
    that column."""
    parent_updates: tuple[ParentUpdate, ...] = ()
    """What parents added, in the order it came: the last is the latest."""
    capture_id: str | None = None
    """The homework note the request is about, when it is about one. Only the id: her
    note's words are never copied here."""
    capture_reference_unreadable: bool = False
    """The request is about a note, and what the file holds in the id's place is no id as
    this store writes one. The request stands and says its note is unavailable; what the
    column holds is never read into text, so nothing of it can reach a page or an answer."""

    @property
    def open(self) -> bool:
        """True until a parent resolves it."""
        return self.state != "resolved"


class RequestClosed(RuntimeError):
    """Raised when a request is asked to move from a state it has left."""

    def __init__(self, request: HelpRequest, wanted: str) -> None:
        super().__init__(
            f"request {request.request_id!r} is {request.state}, so it cannot be {wanted}"
        )
        self.request = request


class AlreadyTakenUp(RuntimeError):
    """Raised for I can help on a request a parent has taken up, with words that are not among
    its updates: nothing is written, and the words are the caller's to keep."""

    def __init__(self, request: HelpRequest) -> None:
        super().__init__(f"request {request.request_id!r} is already taken up")
        self.request = request


class NotTakenUp(RuntimeError):
    """Raised for an update to a request nobody has taken up yet: nothing is written."""

    def __init__(self, request: HelpRequest) -> None:
        super().__init__(f"request {request.request_id!r} is not taken up yet")
        self.request = request


class UpdateFormUsed(RuntimeError):
    """Raised when the form's id already added an update with other words: nothing is
    written, so those words are neither lost nor added in that update's place."""

    def __init__(self, request: HelpRequest) -> None:
        super().__init__(f"the form already added other words to request {request.request_id!r}")
        self.request = request


class UpdateWithoutWords(ValueError):
    """Raised for an update with no words: an update is its words, so nothing is written."""


class UpdateTooLong(ValueError):
    """Raised for a parent's words past the cap, counted as they would be kept: nothing is read
    or written, and the words are the caller's to keep."""

    def __init__(self, length: int) -> None:
        super().__init__(f"an update is at most {NOTE_MAX_LENGTH} characters; this is {length}")
        self.length = length


HELP_REQUEST_COLUMNS: Final = "PRAGMA table_info(help_requests)"
UPDATES_COLUMN: Final = "ALTER TABLE help_requests ADD COLUMN parent_updates TEXT"
EARLIER_REPLY_ROWS: Final = "SELECT rowid FROM help_requests WHERE typeof(response) = 'text'"
"""The requests that hold a reply, by rowid alone, so no words are read until each row is."""
EARLIER_REPLY: Final = """
    UPDATE help_requests
    SET parent_updates = json_insert(
            COALESCE(parent_updates, '[]'),
            '$[#]',
            json_object('id', ?, 'body', ?, 'written_at', NULL)
        ),
        response = ?
    WHERE rowid = ? AND parent_updates IS ?
"""
"""A reply no update holds appended as the request's latest update, with no time, and kept as
the reply too, while the updates are still the ones read."""
TAKE_UP: Final = """
    UPDATE help_requests SET state = 'accepted', accepted_at = ?
    WHERE request_id = ? AND state = 'requested'
"""
ADD_UPDATE: Final = """
    UPDATE help_requests
    SET parent_updates = json_insert(
            COALESCE(parent_updates, '[]'),
            '$[#]',
            json_object('id', ?, 'body', ?, 'written_at', ?)
        ),
        response = ?
    WHERE request_id = ? AND state = ?
      AND NOT EXISTS (
          SELECT 1 FROM json_each(COALESCE(parent_updates, '[]'))
          WHERE json_extract(value, '$.id') = ?
      )
"""
"""One update appended in one statement, which holds only while the request is in the state
given and the form's id is not on it yet."""
CLOSE: Final = """
    UPDATE help_requests
    SET state = 'resolved', resolved_at = ?, accepted_at = COALESCE(accepted_at, ?)
    WHERE request_id = ? AND state <> 'resolved'
"""
INSERT_HELP_REQUEST: Final = """
    INSERT INTO help_requests (request_id, evening, asked_at, note, state, capture_id)
    VALUES (?, ?, ?, ?, 'requested', ?)
"""
NOTES_TABLE: Final = """
    SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'homework_captures'
"""
NOTE_NAMED: Final = "SELECT 1 FROM homework_captures WHERE capture_id = ?"
NOTES_NAMED_TABLE: Final = (
    "CREATE TABLE IF NOT EXISTS notes_named_by_requests (capture_id TEXT PRIMARY KEY)"
)
NOTES_NAMED_FROM_REQUESTS: Final = """
    INSERT OR IGNORE INTO notes_named_by_requests (capture_id)
    SELECT capture_id FROM help_requests WHERE capture_id IS NOT NULL
"""
NOTE_NAMED_ONCE: Final = "INSERT OR IGNORE INTO notes_named_by_requests (capture_id) VALUES (?)"
IDS_TABLE: Final = "CREATE TABLE IF NOT EXISTS help_request_ids (request_id TEXT PRIMARY KEY)"
IDS_FROM_REQUESTS: Final = (
    "INSERT OR IGNORE INTO help_request_ids (request_id) SELECT request_id FROM help_requests"
)
RESERVE_ID: Final = "INSERT OR IGNORE INTO help_request_ids (request_id) VALUES (?)"
ID_KEPT: Final = "SELECT 1 FROM help_request_ids WHERE request_id=?"
RETAINED_ONE: Final = """
    SELECT * FROM help_requests
    WHERE request_id=? AND (state<>'resolved' OR resolved_at >= ?)
"""
RETAINED_ALL: Final = """
    SELECT * FROM help_requests
    WHERE state<>'resolved' OR resolved_at >= ?
"""
OPEN_ROWS: Final = """
    SELECT * FROM help_requests
    WHERE state<>'resolved'
    ORDER BY asked_at, request_id
"""
RESOLVED_ROWS: Final = """
    SELECT * FROM help_requests
    WHERE state='resolved' AND resolved_at >= ?
    ORDER BY resolved_at DESC, request_id
"""
REQUEST_ID: Final = re.compile(r"[0-9a-f]{32}")
"""The shape of an id a form carries: 32 lowercase hex digits, as ``new_request_id`` makes."""


class NotARequestId(ValueError):
    """Raised for an id no form of these pages carries."""


class UnreadableHelpRequest(ValueError):
    """Raised when a kept row cannot be read as a request. Its message names the request
    when its id has the shape a form carries, and the refusal in names alone, so saying it
    gives out none of her words."""

    def __init__(self, why: str = "", held_id: object = None) -> None:
        named = (
            f"the help request {held_id}"
            if type(held_id) is str and REQUEST_ID.fullmatch(held_id)
            else "a kept help request"
        )
        super().__init__(f"{named} cannot be read: {why}" if why else f"{named} cannot be read")


@dataclass(frozen=True)
class HelpHeld:
    """Every request kept, from one statement, and the instant its cutoff was taken from, so
    whatever is worked out from their ages uses that same moment."""

    requests: tuple[HelpRequest, ...]
    now: datetime
    unreadable: int = 0
    """How many kept rows were set apart because they can't be read as requests."""


@dataclass(frozen=True)
class HelpListed:
    """Her requests as the lists show them: those still open, oldest first, then those resolved
    within retention, most recently resolved first, and how many kept rows were set apart
    because they can't be read as requests."""

    open: tuple[HelpRequest, ...]
    resolved: tuple[HelpRequest, ...]
    unreadable: int

    def every(self) -> list[HelpRequest]:
        """Every request that can be read, in the order the lists show them."""
        return [*self.open, *self.resolved]


def new_request_id() -> str:
    """A fresh id for one form that asks for help. Making one writes nothing."""
    return uuid4().hex


def request_id_from(value: str) -> str:
    """The id a form sent, held to the shape ``new_request_id`` makes, or ``NotARequestId``."""
    if REQUEST_ID.fullmatch(value) is None:
        msg = "not a help request id"
        raise NotARequestId(msg)
    return value


def new_update_id() -> str:
    """A fresh id for one form that moves a request: I can help, Add an update, or Close
    request. Making one writes nothing."""
    return uuid4().hex


def update_id_from(value: str) -> str:
    """The id a parent's form sent, held to the shape ``new_update_id`` makes, or
    ``NotARequestId``, as for any id no form of these pages carries."""
    if REQUEST_ID.fullmatch(value) is None:
        msg = "not a form id"
        raise NotARequestId(msg)
    return value


@dataclass(frozen=True)
class HelpAsked:
    """A request made now, under the form's id."""

    request: HelpRequest


@dataclass(frozen=True)
class HelpAlreadyAsked:
    """The same form sent again: the request it made, as it stands now. Nothing is written."""

    request: HelpRequest


@dataclass(frozen=True)
class HelpFormChanged:
    """The form's id made a request with other words or about another note. Nothing is
    written."""

    request: HelpRequest


@dataclass(frozen=True)
class HelpFormUsed:
    """The form's id made a request that was taken back or has gone. Nothing is written."""


AskOutcome = HelpAsked | HelpAlreadyAsked | HelpFormChanged | HelpFormUsed


class UnknownCaptureReference(LookupError):
    """A request named a homework note the record does not have. A name like that proves
    nothing about the page it came from, so nothing is written for it."""


class HelpRequestsStore:
    """SQLite-backed help requests, shared across threads behind a lock."""

    name = "help_requests"
    retention_policy = (
        "Keep a request, with a parent's updates on it, until a parent closes it, and for "
        "fourteen days after, so the updates can be read; nothing older stays, and nothing is "
        "ever derived from how often she asks. The id of a note a request named is kept after "
        "the request goes, and nothing else of it, so that note is never deleted. The id of "
        "every request is kept for good, and nothing else of it, so a form sent again never "
        "asks twice."
    )

    def __init__(self, connection: sqlite3.Connection, clock: Clock) -> None:
        self._connection = connection
        self._connection.row_factory = sqlite3.Row
        self._connection.text_factory = text_or_refusal
        self._clock = clock
        self._lock = threading.Lock()
        self._connection.execute(
            """
            CREATE TABLE IF NOT EXISTS help_requests (
                request_id TEXT PRIMARY KEY,
                evening TEXT NOT NULL,
                asked_at TEXT NOT NULL,
                note TEXT,
                state TEXT NOT NULL,
                accepted_at TEXT,
                resolved_at TEXT,
                response TEXT,
                capture_id TEXT,
                parent_updates TEXT
            )
            """
        )
        # A file from before has the table without one or both of the last columns. Each
        # nullable column is added, once: every request already there reads as about no
        # note and with no updates, and a start that meets the columns again adds nothing.
        columns = {str(row[1]) for row in self._connection.execute(HELP_REQUEST_COLUMNS)}
        if "capture_id" not in columns:
            self._connection.execute("ALTER TABLE help_requests ADD COLUMN capture_id TEXT")
        if "parent_updates" not in columns:
            self._connection.execute(UPDATES_COLUMN)
        self._connection.commit()
        # The notes a request ever named, and the ids every request was asked under, by
        # id alone. Taking a request back or sweeping it leaves both. Each start fills
        # them from the requests still here, in one transaction with the tables, and in
        # the same transaction a reply that no update holds becomes its request's latest.
        self._connection.execute("BEGIN IMMEDIATE")
        try:
            self._connection.execute(NOTES_NAMED_TABLE)
            self._connection.execute(NOTES_NAMED_FROM_REQUESTS)
            self._connection.execute(IDS_TABLE)
            self._connection.execute(IDS_FROM_REQUESTS)
            self._take_in_earlier_replies()
            self._connection.commit()
        except BaseException:
            self._connection.rollback()
            raise

    def _take_in_earlier_replies(self) -> None:
        """At a start, inside its transaction: a reply that no update holds becomes its
        request's latest update, with no time. That is a reply kept from before updates, or
        words a build that reads only the reply saved since the last start.

        Each row is read as the pages read it, and only a request they can read, with words an
        update can hold, gains one: any other row is left as it is, and never stops the start,
        its refusal logged by kind alone. A reply that is the latest update's words, with other
        line endings or edges, adds nothing. The words are kept as every update's are, and the
        reply is set to them, so the two agree."""
        for (rowid,) in self._connection.execute(EARLIER_REPLY_ROWS).fetchall():
            try:
                row = self._connection.execute(
                    "SELECT * FROM help_requests WHERE rowid = ?", (rowid,)
                ).fetchone()
                request = request_from(row)
                words = normalize_note(request.response)
                updates = request.parent_updates
                if words is None or (updates and normalize_note(updates[-1].body) == words):
                    continue
                update = ParentUpdate(update_id=new_update_id(), body=words)
                self._connection.execute(
                    EARLIER_REPLY,
                    (update.update_id, update.body, update.body, rowid, row["parent_updates"]),
                )
            except (sqlite3.Error, ValueError, TypeError, RecursionError) as fault:
                logger.warning("a start left a reply where it was: %s", type(fault).__name__)

    @classmethod
    def open(cls, path: Path, clock: Clock) -> "HelpRequestsStore":
        """Open the file, refusing the places the saved-state store refuses."""
        safe = refuse_unsafe_path(path)
        safe.parent.mkdir(parents=True, exist_ok=True)
        connection = sqlite3.connect(safe, check_same_thread=False)
        connection.execute("PRAGMA secure_delete=ON")
        return cls(connection, clock)

    def close(self) -> None:
        """Close the underlying connection."""
        with self._lock:
            self._connection.close()

    def ask(
        self, evening: date, note: str | None = None, *, capture_id: str | None = None
    ) -> HelpRequest:
        """Keep one request under a fresh id, asked now about ``evening``, and about one note
        when it names one: ``ask_once`` for a caller with no form to send twice."""
        outcome = self.ask_once(new_request_id(), evening, note, capture_id=capture_id)
        if not isinstance(outcome, HelpAsked):
            msg = "a fresh help request id was already used"
            raise RuntimeError(msg)
        return outcome.request

    def ask_once(
        self,
        request_id: str,
        evening: date,
        note: str | None = None,
        *,
        capture_id: str | None = None,
    ) -> AskOutcome:
        """Keep one request for the form whose id this is, and never a second.

        The id and a name are held to their shapes before anything is read.
        Then the writer is reserved on this store's own connection, and in
        that one transaction the id is reserved, the note is looked for, the
        request is written and the note's id is kept as asked about. An id
        already used is not a new request: the same form sent again, with the
        same words about the same note, is the request it made, as it stands;
        other words or another note is a form that already asked; and an id
        whose request was taken back or has gone asks nothing again. A name
        that is no note of this record, including a deleted note's id, is
        ``UnknownCaptureReference``. Whatever the file refuses is rolled back
        whole, and the id is left unused.
        """
        form = request_id_from(request_id)
        name = None if capture_id is None else capture_id_from(capture_id)
        request = HelpRequest(
            request_id=form,
            evening=evening,
            asked_at=self._clock.now(),
            note=note,
            capture_id=name,
        )
        with self._lock:
            self._connection.execute("BEGIN IMMEDIATE")
            try:
                if self._connection.execute(RESERVE_ID, (form,)).rowcount != 1:
                    outcome = self._asked_before(form, note, name)
                    self._connection.rollback()
                    return outcome
                if name is not None and not self._note_on_record(name):
                    raise UnknownCaptureReference(name)
                self._connection.execute(
                    INSERT_HELP_REQUEST,
                    (
                        request.request_id,
                        evening.isoformat(),
                        request.asked_at.isoformat(),
                        note,
                        name,
                    ),
                )
                if name is not None:
                    self._connection.execute(NOTE_NAMED_ONCE, (name,))
                self._connection.commit()
            except BaseException:
                self._connection.rollback()
                raise
        return HelpAsked(request)

    def already_asked(
        self, request_id: str, note: str | None = None, *, capture_id: str | None = None
    ) -> AskOutcome | None:
        """What sending the form whose id this is would answer, or None while its id is
        unused. It reads and never writes: ``ask_once`` decides again in its own
        transaction, so a caller may answer a form sent again before reading anything else."""
        form = request_id_from(request_id)
        name = None if capture_id is None else capture_id_from(capture_id)
        with self._lock:
            if self._connection.execute(ID_KEPT, (form,)).fetchone() is None:
                return None
            return self._asked_before(form, note, name)

    def _asked_before(self, form: str, note: str | None, name: str | None) -> AskOutcome:
        """What an id already used stands for, read through this store's own connection."""
        row = self._held_row(RETAINED_ONE, (form, self._cutoff()))
        if row is None:
            return HelpFormUsed()
        standing = request_from(row)
        same_words = spaced(standing.note) == spaced(note)
        same_note = standing.capture_id == name and not standing.capture_reference_unreadable
        if same_words and same_note:
            return HelpAlreadyAsked(standing)
        return HelpFormChanged(standing)

    def _note_on_record(self, capture_id: str) -> bool:
        """Whether the file holds a note of this id, read through this connection, inside the
        caller's transaction. A file with no notes in it holds none."""
        if self._connection.execute(NOTES_TABLE).fetchone() is None:
            return False
        return self._connection.execute(NOTE_NAMED, (capture_id,)).fetchone() is not None

    def take_back(self, request_id: str) -> bool:
        """Remove a request nobody has taken up yet. False when there is none to remove.

        Once a parent has taken it up, the request is theirs to resolve; taking
        it back then is refused, since the parent may already be on it. The
        request is read with the retention cutoff, as ``get`` reads it, so one
        resolved past retention that the sweep has not reached yet is none. A
        row that can't be read as a request is ``UnreadableHelpRequest``, and
        stays as it is.
        """
        with self._lock, self._connection:
            row = self._held_row(RETAINED_ONE, (request_id, self._cutoff()))
            if row is None:
                return False
            current = request_from(row)
            if current.state != "requested":
                raise RequestClosed(current, "taken back")
            self._connection.execute("DELETE FROM help_requests WHERE request_id=?", (request_id,))
        return True

    def accept(
        self, request_id: str, response: str | None = None, *, update_id: str | None = None
    ) -> HelpRequest:
        """A parent takes the request up, with any words as its first update.

        A second I can help adds nothing. Sent with no words, with words
        already among the updates, or as the same form again, it is the
        request as it stands; other words are ``AlreadyTakenUp``, so they are
        neither added twice nor lost. A closed request is ``RequestClosed``,
        unless this form's words are on it already. ``update_id`` is the
        form's id; with none, the move counts as a form of its own.
        """
        words, form = kept_words(response), form_id(update_id)
        with self._writing() as now:
            current = self._read(request_id, now)
            if current.state == "requested":
                self._connection.execute(TAKE_UP, (now.isoformat(), request_id))
                if words is not None:
                    update = ParentUpdate(update_id=form, body=words, written_at=now)
                    self._append(request_id, "accepted", update)
                return self._read(request_id, now)
            if retried(current, form, words):
                return current
            if current.state == "resolved":
                raise RequestClosed(current, "taken up")
            if words is None or on_record(current, words):
                return current
            raise AlreadyTakenUp(current)

    def add_update(
        self, request_id: str, body: str | None, *, update_id: str | None = None
    ) -> HelpRequest:
        """A parent adds words to a request a parent has taken up, and it stays open.

        The words are appended in one statement that holds only while the
        request is taken up and the form's id is not on it yet. The same form
        sent again with the same words is the request as it stands; with other
        words it is ``UpdateFormUsed``. A request that is not kept is a
        ``KeyError`` before its words are looked at; then no words at all is
        ``UpdateWithoutWords``, a closed request ``RequestClosed``, and one
        nobody has taken up ``NotTakenUp``. Each writes nothing.
        """
        words, form = kept_words(body), form_id(update_id)
        with self._writing() as now:
            current = self._read(request_id, now)
            if words is None:
                msg = "an update has no words"
                raise UpdateWithoutWords(msg)
            update = ParentUpdate(update_id=form, body=words, written_at=now)
            if self._append(request_id, "accepted", update):
                return self._read(request_id, now)
            if retried(current, form, words):
                return current
            if current.state == "resolved":
                raise RequestClosed(current, "given an update")
            if current.state == "requested":
                raise NotTakenUp(current)
            raise UpdateFormUsed(current)

    def resolve(
        self, request_id: str, response: str | None = None, *, update_id: str | None = None
    ) -> HelpRequest:
        """A parent closes the request, from either open state, any words added first as its
        latest update.

        A closed request closed again changes nothing when the close carries
        no words or is this form's again; other words are ``RequestClosed``,
        for the caller to keep. A form whose id already added other words
        closes nothing and is ``UpdateFormUsed``.
        """
        words, form = kept_words(response), form_id(update_id)
        with self._writing() as now:
            current = self._read(request_id, now)
            if current.state == "resolved":
                if words is None or retried(current, form, words):
                    return current
                raise RequestClosed(current, "resolved again")
            if words is not None and not retried(current, form, words):
                if any(added.update_id == form for added in current.parent_updates):
                    raise UpdateFormUsed(current)
                update = ParentUpdate(update_id=form, body=words, written_at=now)
                self._append(request_id, current.state, update)
            self._connection.execute(CLOSE, (now.isoformat(), now.isoformat(), request_id))
            return self._read(request_id, now)

    @contextmanager
    def _writing(self) -> Iterator[datetime]:
        """One move under the lock, with the file's writer reserved before anything is read and
        the move's instant read once it is, so times follow the order moves are kept in:
        committed whole when the move returns, rolled back whole when it raises."""
        with self._lock:
            self._connection.execute("BEGIN IMMEDIATE")
            try:
                yield self._clock.now()
                self._connection.commit()
            except BaseException:
                self._connection.rollback()
                raise

    def _append(self, request_id: str, state: HelpState, update: ParentUpdate) -> bool:
        """Append one update inside the caller's transaction, while the request is in
        ``state`` and the update's form id is not on it, with its words as the reply's too;
        whether it was appended."""
        written = None if update.written_at is None else update.written_at.isoformat()
        appended = self._connection.execute(
            ADD_UPDATE,
            (
                update.update_id,
                update.body,
                written,
                update.body,
                request_id,
                state,
                update.update_id,
            ),
        )
        return appended.rowcount == 1

    def get(self, request_id: str) -> HelpRequest | None:
        """One request by id, or ``None``, a resolved one past retention counting as none.

        The cutoff applies on every read, so a request that has aged out is
        gone the moment it ages out, whether or not a sweep has run since.
        """
        with self._lock:
            row = self._held_row(RETAINED_ONE, (request_id, self._cutoff()))
        return None if row is None else request_from(row)

    def open_requests(self) -> list[HelpRequest]:
        """Every request not yet resolved that can be read, oldest first: what a parent has to
        act on. A row that can't be read is set apart, as ``listed`` counts it."""
        with self._lock:
            rows = self._held_rows(OPEN_ROWS, ())
        return list(readable(rows)[0])

    def recently_resolved(self) -> list[HelpRequest]:
        """Every request resolved within retention that can be read, most recent first: words
        back she can read. A row that can't be read is set apart, as ``listed`` counts it."""
        with self._lock:
            rows = self._held_rows(RESOLVED_ROWS, (self._cutoff(),))
        return list(readable(rows)[0])

    def listed(self) -> HelpListed:
        """The open requests and those resolved within retention, read under one hold of the
        lock, with how many kept rows were set apart because they can't be read. A statement
        the file refuses is raised, never an empty list."""
        with self._lock:
            open_rows = self._held_rows(OPEN_ROWS, ())
            resolved_rows = self._held_rows(RESOLVED_ROWS, (self._cutoff(),))
        waiting, open_unread = readable(open_rows)
        resolved, resolved_unread = readable(resolved_rows)
        return HelpListed(waiting, resolved, open_unread + resolved_unread)

    def retained(self) -> HelpHeld:
        """Every request kept, open or resolved within retention, in one statement, with the
        instant the cutoff was taken from. It reads and never writes, so reading them resets
        nothing. A row that can't be read as a request is set apart and counted."""
        with self._lock:
            now = self._clock.now()
            rows = self._held_rows(RETAINED_ALL, (self._cutoff(now),))
        requests, unreadable = readable(rows)
        return HelpHeld(requests, now, unreadable)

    def sweep(self) -> int:
        """Delete every resolved request past retention; return how many went."""
        with self._lock, self._connection:
            removed = self._connection.execute(
                "DELETE FROM help_requests WHERE state='resolved' AND resolved_at < ?",
                (self._cutoff(),),
            ).rowcount
        return int(removed)

    def _read(self, request_id: str, now: datetime | None = None) -> HelpRequest:
        """One request inside a held lock, or a ``KeyError`` for one that does not exist.

        A resolved request past retention does not exist here either, so no
        move can be made on it. ``now`` is the move's instant, when it has one.
        """
        row = self._held_row(RETAINED_ONE, (request_id, self._cutoff(now)))
        if row is None:
            msg = f"no help request {request_id!r}"
            raise KeyError(msg)
        return request_from(row)

    def _held_rows(self, statement: str, parameters: tuple[object, ...]) -> list[sqlite3.Row]:
        """The rows a read returns with each text column as its stored bytes, ``HeldText``, so
        text that is not UTF-8 is found as each row is built, one request's to answer for, and
        not as the rows are fetched. The caller holds the lock; the connection's own way of
        reading text is put back however the read ends."""
        kept = self._connection.text_factory
        self._connection.text_factory = HeldText
        try:
            return list(self._connection.execute(statement, parameters).fetchall())
        finally:
            self._connection.text_factory = kept

    def _held_row(self, statement: str, parameters: tuple[object, ...]) -> sqlite3.Row | None:
        """The one row a read by id returns, as ``_held_rows`` reads it, or ``None``."""
        rows = self._held_rows(statement, parameters)
        return rows[0] if rows else None

    def _cutoff(self, now: datetime | None = None) -> str:
        """The oldest resolution still within retention, by the store's clock, or from ``now``
        when the caller has read the clock already."""
        read = self._clock.now() if now is None else now
        return (read - timedelta(days=HELP_RETENTION_DAYS)).isoformat()


def spaced(words: str | None) -> str:
    """Words with every run of spaces, tabs and line breaks said once, for telling a form sent
    again from one with other words."""
    return " ".join((words or "").split())


def kept_words(words: str | None) -> str | None:
    """A parent's words as every move keeps them, as the family page's box counts them: one kind
    of line ending, the edges trimmed, ``None`` for none or blank, and ``UpdateTooLong`` past
    the cap."""
    kept = normalize_note(words)
    if kept is not None and len(kept) > NOTE_MAX_LENGTH:
        raise UpdateTooLong(len(kept))
    return kept


def form_id(update_id: str | None) -> str:
    """The id of the form a move came from, held to its shape, or a fresh one for a caller with
    no form to send twice."""
    return new_update_id() if update_id is None else update_id_from(update_id)


def retried(request: HelpRequest, form: str, words: str | None) -> bool:
    """Whether this form's id already added these words to the request, spaced alike or not:
    the same form sent again."""
    return any(
        update.update_id == form and spaced(update.body) == spaced(words)
        for update in request.parent_updates
    )


def on_record(request: HelpRequest, words: str) -> bool:
    """Whether these words, spaced alike or not, are among the request's updates already."""
    return any(spaced(update.body) == spaced(words) for update in request.parent_updates)


def updates_from(held: object) -> tuple[ParentUpdate, ...]:
    """A request's updates from what the file holds: none for nothing held, else the list of
    updates as this store writes it. Anything else is ``ValueError`` or ``TypeError``, in
    words that repeat none of what is held."""
    if held is None:
        return ()
    if type(held) is not str:
        msg = "the updates are held as no text"
        raise TypeError(msg)
    items = json.loads(held)
    if type(items) is not list:
        msg = "the updates are held as no list"
        raise TypeError(msg)
    updates = []
    for item in items:
        if type(item) is not dict:
            msg = "an update is held as no object"
            raise TypeError(msg)
        held_as = {"update_id": "id", "body": "body", "written_at": "written_at"}
        updates.append(
            ParentUpdate.model_validate({name: item.get(key) for name, key in held_as.items()})
        )
    return tuple(updates)


def request_from(row: sqlite3.Row) -> HelpRequest:
    """Build a request from a row read by column name, its text held as bytes or not, or
    ``UnreadableHelpRequest``, raised after the except block so the refusal, which can repeat
    her words, goes nowhere. Text that is not UTF-8 is refused like any other field, except
    in the note's place, where the request stands with its note marked unavailable."""

    def when(value: object) -> datetime | None:
        return None if value is None else datetime.fromisoformat(str(decoded(value)))

    try:
        request_id = str(decoded(row["request_id"]))
        note = decoded(row["note"])
        response = decoded(row["response"])
        capture_id, unreadable = reference_from(held_reference(row["capture_id"]), request_id)
        return HelpRequest(
            request_id=request_id,
            evening=date.fromisoformat(str(decoded(row["evening"]))),
            asked_at=datetime.fromisoformat(str(decoded(row["asked_at"]))),
            note=None if note is None else str(note),
            state=cast(HelpState, str(decoded(row["state"]))),
            accepted_at=when(row["accepted_at"]),
            resolved_at=when(row["resolved_at"]),
            response=None if response is None else str(response),
            parent_updates=updates_from(decoded(row["parent_updates"])),
            capture_id=capture_id,
            capture_reference_unreadable=unreadable,
        )
    except (ValueError, TypeError, RecursionError) as fault:
        why = refusal_in_names(fault, HelpRequest.model_fields)
    held_id = row["request_id"]
    named = held_id.decode("utf-8", "replace") if isinstance(held_id, HeldText) else held_id
    raise UnreadableHelpRequest(why, named)


def decoded(value: object) -> object:
    """A column as a row holds it, with text held as bytes decoded as UTF-8, strictly: bytes
    that are not UTF-8 raise ``UnicodeDecodeError``, a ``ValueError``, for that row."""
    return value.decode("utf-8") if isinstance(value, HeldText) else value


def held_reference(value: object) -> object:
    """What the note's place holds, as ``reference_from`` reads it: text decoded, and text
    that is not UTF-8 left as plain bytes, which is no id."""
    try:
        return decoded(value)
    except UnicodeDecodeError:
        return bytes(cast(bytes, value))


def readable(rows: list[sqlite3.Row]) -> tuple[tuple[HelpRequest, ...], int]:
    """Each row built as a request, in order, and how many were set apart because they can't be
    read. Each is logged by its refusal in names, which holds none of her words; nothing about
    it is kept or repaired."""
    requests: list[HelpRequest] = []
    unreadable = 0
    for row in rows:
        try:
            requests.append(request_from(row))
        except UnreadableHelpRequest as refused:
            logger.warning("a kept help request was set apart: %s", refused)
            unreadable += 1
    return tuple(requests), unreadable


def reference_from(held: object, request_id: str) -> tuple[str | None, bool]:
    """The note a request is about, from what the file holds: the id, and whether something
    is held that is no id.

    Nothing held is about no note. A ``str`` that is a note's id in the one
    spelling this store writes is that id. Anything else, bytes, other
    words, a number, an id in capitals, was not written here. It is not
    turned into text, since ``str`` of corrupted bytes is printable and
    would be handed on as an id; the request is kept and marked instead,
    and the log names the request and the type held, never the content.
    """
    if held is None:
        return None, False
    if type(held) is str:
        try:
            if capture_id_from(held) == held:
                return held, False
        except ValueError:
            pass
    logger.warning(
        "help request %s holds a %s that is no note id in the place of one",
        request_id,
        type(held).__name__,
    )
    return None, True
