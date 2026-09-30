"""Her request for help, and what a parent did with it, kept where she can see both.

Asking for help is a gesture like the "too much" signal: one press, with a
sentence if she wants one and no form to fill. Unlike the signal, it is
addressed to a person, so it has a state a person moves: requested, until a
parent takes it up; accepted, while the parent is on it; resolved, with a
word back if they left one. Each step shows on her page in plain words, and
nothing says a parent is looking into something before that parent has said
so. She can take a request back while nobody has taken it up.

The store keeps a resolved request for two weeks, long enough for the word
back to be read, and applies that cutoff on every read as well as in the
sweep. An open request is kept until someone resolves it: a request is a
question to a person, and a question nobody has answered is not old news.
Nothing here counts requests or groups them by anything; a record like that
would be about her rather than about the help.

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

import logging
import re
import sqlite3
import threading
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Final, Literal, cast
from uuid import uuid4

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field

from blossom.captures import capture_id_from
from blossom.clock import Clock
from blossom.stores.paths import refuse_unsafe_path
from blossom.unreadable import refusal_in_names, text_or_refusal

logger = logging.getLogger(__name__)

HELP_RETENTION_DAYS: Final = 14
"""How long a resolved request is kept: long enough for the word back to be read."""

HELP_RECENT_DAYS: Final = 7
"""How long a resolved request stays among her help updates, counted from when it was
resolved. After that it is kept apart, with those resolved earlier, until retention
takes it."""

NOTE_MAX_LENGTH: Final = 500
"""The most that is kept of her note or a parent's word back: a sentence or two,
the same cap as everywhere else words are typed into this application."""

HelpState = Literal["requested", "accepted", "resolved"]
"""Where a request stands: asked and not yet taken up, taken up by a parent, or
answered. Only a parent moves it forward; only she takes it back, and only
while it is still just requested."""


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
    """The parent's word back, left when taking the request up or resolving it."""
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


HELP_REQUEST_COLUMNS: Final = "PRAGMA table_info(help_requests)"
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


def new_request_id() -> str:
    """A fresh id for one form that asks for help. Making one writes nothing."""
    return uuid4().hex


def request_id_from(value: str) -> str:
    """The id a form sent, held to the shape ``new_request_id`` makes, or ``NotARequestId``."""
    if REQUEST_ID.fullmatch(value) is None:
        msg = "not a help request id"
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
        "Keep a request until a parent resolves it, and for fourteen days after, so the word "
        "back can be read; nothing older stays, and nothing is ever derived from how often "
        "she asks. The id of a note a request named is kept after the request goes, and "
        "nothing else of it, so that note is never deleted. The id of every request is kept "
        "for good, and nothing else of it, so a form sent again never asks twice."
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
                capture_id TEXT
            )
            """
        )
        # A file from before has the table without the last column. One nullable column
        # is added, once: every request already there reads as about no note, and a
        # start that meets the column again adds nothing.
        columns = {str(row[1]) for row in self._connection.execute(HELP_REQUEST_COLUMNS)}
        if "capture_id" not in columns:
            self._connection.execute("ALTER TABLE help_requests ADD COLUMN capture_id TEXT")
        self._connection.commit()
        # The notes a request ever named, and the ids every request was asked under, by
        # id alone. Taking a request back or sweeping it leaves both. Each start fills
        # them from the requests still here, in one transaction with the tables.
        self._connection.execute("BEGIN IMMEDIATE")
        try:
            self._connection.execute(NOTES_NAMED_TABLE)
            self._connection.execute(NOTES_NAMED_FROM_REQUESTS)
            self._connection.execute(IDS_TABLE)
            self._connection.execute(IDS_FROM_REQUESTS)
            self._connection.commit()
        except BaseException:
            self._connection.rollback()
            raise

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
        row = self._connection.execute(RETAINED_ONE, (form, self._cutoff())).fetchone()
        if row is None:
            return HelpFormUsed()
        standing = request_from(row)
        same_words = spaced(row["note"]) == spaced(note)
        if same_words and row["capture_id"] == name:
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
        resolved past retention that the sweep has not reached yet is none.
        """
        with self._lock, self._connection:
            row = self._connection.execute(RETAINED_ONE, (request_id, self._cutoff())).fetchone()
            if row is None:
                return False
            if row["state"] != "requested":
                raise RequestClosed(request_from(row), "taken back")
            self._connection.execute("DELETE FROM help_requests WHERE request_id=?", (request_id,))
        return True

    def accept(self, request_id: str, response: str | None = None) -> HelpRequest:
        """A parent takes the request up, with a word back if given.

        Taking up one already taken up changes nothing, words included: the
        word she has already read stays, whatever comes with the repeat.
        """
        stamp = self._clock.now().isoformat()
        with self._lock, self._connection:
            current = self._read(request_id)
            if current.state == "resolved":
                raise RequestClosed(current, "taken up")
            if current.state == "accepted":
                return current
            self._connection.execute(
                """
                UPDATE help_requests
                SET state='accepted', accepted_at=?, response=?
                WHERE request_id=?
                """,
                (stamp, response, request_id),
            )
            return self._read(request_id)

    def resolve(self, request_id: str, response: str | None = None) -> HelpRequest:
        """A parent answers the request, from either open state, with a word back if given."""
        stamp = self._clock.now().isoformat()
        with self._lock, self._connection:
            current = self._read(request_id)
            if current.state == "resolved":
                raise RequestClosed(current, "resolved again")
            self._connection.execute(
                """
                UPDATE help_requests
                SET state='resolved', resolved_at=?, accepted_at=COALESCE(accepted_at, ?),
                    response=COALESCE(?, response)
                WHERE request_id=?
                """,
                (stamp, stamp, response, request_id),
            )
            return self._read(request_id)

    def get(self, request_id: str) -> HelpRequest | None:
        """One request by id, or ``None``, a resolved one past retention counting as none.

        The cutoff applies on every read, so a request that has aged out is
        gone the moment it ages out, whether or not a sweep has run since.
        """
        with self._lock:
            row = self._connection.execute(
                """
                SELECT * FROM help_requests
                WHERE request_id=? AND (state<>'resolved' OR resolved_at >= ?)
                """,
                (request_id, self._cutoff()),
            ).fetchone()
        return None if row is None else request_from(row)

    def open_requests(self) -> list[HelpRequest]:
        """Every request not yet resolved, oldest first: what a parent has to act on."""
        with self._lock:
            rows = self._connection.execute(
                """
                SELECT * FROM help_requests
                WHERE state<>'resolved'
                ORDER BY asked_at, request_id
                """
            ).fetchall()
        return [request_from(row) for row in rows]

    def recently_resolved(self) -> list[HelpRequest]:
        """Every request resolved within retention, most recent first: words back she can read."""
        with self._lock:
            rows = self._connection.execute(
                """
                SELECT * FROM help_requests
                WHERE state='resolved' AND resolved_at >= ?
                ORDER BY resolved_at DESC, request_id
                """,
                (self._cutoff(),),
            ).fetchall()
        return [request_from(row) for row in rows]

    def retained(self) -> HelpHeld:
        """Every request kept, open or resolved within retention, in one statement, with the
        instant the cutoff was taken from. It reads and never writes, so reading them resets
        nothing. A row that cannot be read as a request is ``UnreadableHelpRequest``."""
        with self._lock:
            now = self._clock.now()
            rows = self._connection.execute(RETAINED_ALL, (self._cutoff(now),)).fetchall()
        return HelpHeld(tuple(request_from(row) for row in rows), now)

    def sweep(self) -> int:
        """Delete every resolved request past retention; return how many went."""
        with self._lock, self._connection:
            removed = self._connection.execute(
                "DELETE FROM help_requests WHERE state='resolved' AND resolved_at < ?",
                (self._cutoff(),),
            ).rowcount
        return int(removed)

    def _read(self, request_id: str) -> HelpRequest:
        """One request inside a held lock, or a ``KeyError`` for one that does not exist.

        A resolved request past retention does not exist here either, so no
        move can be made on it.
        """
        row = self._connection.execute(
            """
            SELECT * FROM help_requests
            WHERE request_id=? AND (state<>'resolved' OR resolved_at >= ?)
            """,
            (request_id, self._cutoff()),
        ).fetchone()
        if row is None:
            msg = f"no help request {request_id!r}"
            raise KeyError(msg)
        return request_from(row)

    def _cutoff(self, now: datetime | None = None) -> str:
        """The oldest resolution still within retention, by the store's clock, or from ``now``
        when the caller has read the clock already."""
        read = self._clock.now() if now is None else now
        return (read - timedelta(days=HELP_RETENTION_DAYS)).isoformat()


def spaced(words: str | None) -> str:
    """Words with every run of spaces, tabs and line breaks said once, for telling a form sent
    again from one with other words."""
    return " ".join((words or "").split())


def request_from(row: sqlite3.Row) -> HelpRequest:
    """Build a request from a row read by column name, or ``UnreadableHelpRequest``, raised
    after the except block so the refusal, which can repeat her words, goes nowhere."""

    def when(value: object) -> datetime | None:
        return None if value is None else datetime.fromisoformat(str(value))

    try:
        note = row["note"]
        response = row["response"]
        capture_id, unreadable = reference_from(row["capture_id"], str(row["request_id"]))
        return HelpRequest(
            request_id=str(row["request_id"]),
            evening=date.fromisoformat(str(row["evening"])),
            asked_at=datetime.fromisoformat(str(row["asked_at"])),
            note=None if note is None else str(note),
            state=cast(HelpState, str(row["state"])),
            accepted_at=when(row["accepted_at"]),
            resolved_at=when(row["resolved_at"]),
            response=None if response is None else str(response),
            capture_id=capture_id,
            capture_reference_unreadable=unreadable,
        )
    except (ValueError, TypeError) as fault:
        why = refusal_in_names(fault, HelpRequest.model_fields)
    raise UnreadableHelpRequest(why, row["request_id"])


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
