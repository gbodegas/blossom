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
"""

import logging
import sqlite3
import threading
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Final, Literal, cast
from uuid import uuid4

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field

from blossom.captures import capture_id_from
from blossom.clock import Clock
from blossom.stores.paths import refuse_unsafe_path

logger = logging.getLogger(__name__)

HELP_RETENTION_DAYS: Final = 14
"""How long a resolved request is kept: long enough for the word back to be read."""

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


class UnknownCaptureReference(LookupError):
    """A request named a homework note the record does not have. A name like that proves
    nothing about the page it came from, so nothing is written for it."""


class HelpRequestsStore:
    """SQLite-backed help requests, shared across threads behind a lock."""

    name = "help_requests"
    retention_policy = (
        "Keep a request until a parent resolves it, and for fourteen days after, so the word "
        "back can be read; nothing older stays, and nothing is ever derived from how often "
        "she asks."
    )

    def __init__(self, connection: sqlite3.Connection, clock: Clock) -> None:
        self._connection = connection
        self._connection.row_factory = sqlite3.Row
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
        """Keep one request, asked now about ``evening``, and about one note when it names one.

        A name is held to the shape of a note's id before anything is read.
        Then the writer is reserved on this store's own connection, the note
        is looked for in the file, and the request is written, all in that
        one transaction: the connection's own scope would begin nothing until
        the insert, which would leave the look outside it. A name that is no
        note of this record is ``UnknownCaptureReference`` and nothing is
        written. Whatever the file refuses is rolled back whole.
        """
        name = None if capture_id is None else capture_id_from(capture_id)
        request = HelpRequest(
            request_id=uuid4().hex,
            evening=evening,
            asked_at=self._clock.now(),
            note=note,
            capture_id=name,
        )
        with self._lock:
            self._connection.execute("BEGIN IMMEDIATE")
            try:
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
                self._connection.commit()
            except BaseException:
                self._connection.rollback()
                raise
        return request

    def _note_on_record(self, capture_id: str) -> bool:
        """Whether the file holds a note of this id, read through this connection, inside the
        caller's transaction. A file with no notes in it holds none."""
        if self._connection.execute(NOTES_TABLE).fetchone() is None:
            return False
        return self._connection.execute(NOTE_NAMED, (capture_id,)).fetchone() is not None

    def take_back(self, request_id: str) -> bool:
        """Remove a request nobody has taken up yet. False when there is none to remove.

        Once a parent has taken it up, the request is theirs to resolve; taking
        it back then is refused, since the parent may already be on it.
        """
        with self._lock, self._connection:
            row = self._connection.execute(
                "SELECT state FROM help_requests WHERE request_id=?", (request_id,)
            ).fetchone()
            if row is None:
                return False
            if row["state"] != "requested":
                raise RequestClosed(self._read(request_id), "taken back")
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

    def _cutoff(self) -> str:
        """The oldest resolution still within retention, by the store's clock."""
        return (self._clock.now() - timedelta(days=HELP_RETENTION_DAYS)).isoformat()


def request_from(row: sqlite3.Row) -> HelpRequest:
    """Build a request from a row read by column name."""

    def when(value: object) -> datetime | None:
        return None if value is None else datetime.fromisoformat(str(value))

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
