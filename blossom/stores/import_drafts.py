# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Gerardo Bodegas Martinez
"""Stored import drafts: screenshots a parent uploaded, what was read from them, and the
review's own progress, kept until the review finishes or expires.

A draft holds inputs and draft-only progress. It never decides what was saved: that is
the acceptance records the record store writes in its own save transaction. This store
reads those records, inside each of its own transactions, to tell which rows are saved,
and it writes nothing outside its own six tables. It also keeps the review's answers and
marks until the draft closes, and refuses any change to the screenshots once some homework
from them is saved.

A draft's rows come from the whole set of its stored readings, so the store is given,
at construction, one pure function per draft kind from those readings to the row keys.
A kind with no function can't make a draft, and the sweep applies expiry alone to it.
Two more pure functions per kind may be given: what a change of readings does to the
review's answers and set-aside rows, run inside the change's own transaction, and each
row's piece names, which tell a row split into pieces saved once all of them are.

Every call is one transaction. Expiry is 48 hours after creation on the real clock,
never the household's pinned date. Unavailable and removed are two steps: from the instant
of expiry every call refuses the draft, by the cutoff each one applies, while its rows stay
in the file until the first sweep after it, or the next start. A Read attempt's deadline is
an instant on the process's monotonic clock, so every start clears each attempt before
anything is served.
"""

import hashlib
import json
import logging
import re
import secrets
import sqlite3
import threading
import time
from collections.abc import Callable, Collection, Iterator, Mapping, Sequence
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Final, Literal, NamedTuple, cast

from blossom.clock import Clock
from blossom.stores.drafts import StoreBusy, WriterBusy
from blossom.stores.paths import refuse_unsafe_path
from blossom.unreadable import TextNotUtf8, text_or_named_refusal

logger = logging.getLogger(__name__)

DRAFT_LIFETIME: Final = timedelta(hours=48)
"""How long a draft lives from its creation, fixed: nothing extends it."""

MOST_IMAGES_PRESENT: Final = 10
"""The most images a review holds at a time. Removing one frees a place."""

READ_DEADLINE_SECONDS: Final = 120.0
"""How long after a Read press its attempt stays current, covering every image and retry."""

STORE_WAIT_SECONDS: Final = 5.0
"""The longest one call waits for the store and the file's writer together, unless its
caller gives it less."""

KINDS: Final = frozenset({"homework_screenshot", "grade_screenshot"})
"""The draft kinds, named as the acceptance records name them."""

MADE_BY: Final = frozenset({"parent", "household"})
"""Who made a draft: a signed-in parent, or this computer with sign-in off."""

FAILURES: Final = (
    "not_read_in_time",
    "service_busy",
    "service_refused",
    "model_refused",
    "cut_off",
    "not_json",
    "not_valid",
)
"""The closed set of ways a read can fail. A failed image stays open and can be read again."""

TOKEN_SHAPE: Final = re.compile(r"[0-9a-f]{32}")
"""A creation token's form: 128 random bits as hex."""

RowState = Literal["saved", "discarded", "open"]


class NewImage(NamedTuple):
    """One prepared PNG to add, with its dimensions. Its SHA-256 is taken here."""

    png: bytes
    width: int
    height: int


class UploadedReading(NamedTuple):
    """A validated reading to store: its JSON, never the raw output, with its versions."""

    kind: str
    prompt_version: str
    schema_version: str
    body: str


class StoredReading(NamedTuple):
    """A stored reading as the rows function receives it, with its image's SHA-256."""

    sha256: str
    kind: str
    prompt_version: str
    schema_version: str
    body: str


class DraftRow(NamedTuple):
    """One row key a rows function returns, and whether it is a "couldn't read" item."""

    key: str
    readable: bool


RowsFunction = Callable[[Sequence[StoredReading]], Sequence[DraftRow]]
"""A pure function from all of a draft's stored readings to its row keys. The readings come
in an order fixed by content, never by upload."""


class ReadTiming(NamedTuple):
    """What a read took: seconds and token counts, the only things logged about it."""

    seconds: float
    input_tokens: int
    output_tokens: int


class ImageState(NamedTuple):
    """One image as a review shows it, without its bytes."""

    image_id: str
    position: int
    sha256: str
    width: int
    height: int
    size: int
    added_at: datetime
    read: bool
    failure: str | None
    covered: bool
    """Whether the draft's current attempt covers it."""
    in_ended_attempt: bool
    """Whether the draft's last attempt covered it and has ended. Every start clears it, since
    a stopped process's deadline is never seen to pass."""


class RowView(NamedTuple):
    """One row of the draft and where it stands."""

    key: str
    readable: bool
    state: RowState


class DraftSnapshot(NamedTuple):
    """A draft as one read saw it: what a review assembles from and checks against."""

    draft_id: str
    kind: str
    made_by: str
    version: int
    created_at: datetime
    expires_at: datetime
    closed_at: datetime | None
    page_key: bytes
    progress_revision: int
    progress: str | None
    attempt_ends: float | None
    """The current attempt's deadline on the monotonic clock, or None when none is current."""
    images: tuple[ImageState, ...]
    readings: tuple[StoredReading, ...]
    rows: tuple[RowView, ...]
    saved: frozenset[str]
    """Every key an acceptance under this draft names."""
    discarded: frozenset[str]
    """Every key the draft's discard list names that no acceptance does."""
    finished: bool
    frozen: bool = False
    """Whether some homework from it is saved, so its screenshots stay as they are."""


class OpenDraft(NamedTuple):
    """One open review as a list of them shows it: its id, kind, times and how many images it
    holds. Nothing read from an image, and no row, is in it."""

    draft_id: str
    kind: str
    created_at: datetime
    expires_at: datetime
    images: int


class Added(NamedTuple):
    """What an upload's commit did."""

    draft_id: str
    version: int
    added: tuple[str, ...]
    """The new images' ids, in upload order."""
    duplicates: tuple[int, ...]
    """Places, in the list of images given to the store, of those the draft already held or
    that the list repeated. A caller maps them back to places in its upload."""
    free: int
    """Places left for more images after this commit."""


class Attempt(NamedTuple):
    """A Read attempt: its id, its deadline on the monotonic clock, and the images it covers."""

    attempt_id: str
    ends: float
    covered: tuple[str, ...]


class Swept(NamedTuple):
    """What one sweep did: drafts deleted at expiry, finished drafts closed, and drafts left
    open because whether they were finished couldn't be worked out."""

    expired: int
    closed: int
    passed_over: int = 0


class DraftNotFound(LookupError):
    """No draft of hers has this id: never made, swept, or another student's."""


class DraftExpired(RuntimeError):
    """The draft is still present and past its expiry."""


class DraftClosed(RuntimeError):
    """The draft is closed or finished, and takes no more input."""


class KindNotWired(ValueError):
    """No rows function is wired for this kind, so no draft of it can be made."""


class TooManyImages(RuntimeError):
    """The upload would make more images present than a review holds. Nothing was added."""

    def __init__(self, free: int) -> None:
        super().__init__(f"{free} places are free")
        self.free = free


class UnknownImage(LookupError):
    """The image id is not an image of this draft."""


class AttemptUnderWay(RuntimeError):
    """A Read attempt is still current; no other starts until its deadline."""

    def __init__(self, ends: float) -> None:
        super().__init__("a Read attempt is under way")
        self.ends = ends


class ReviewChanged(RuntimeError):
    """The page's version isn't the draft's, or it names a row the draft doesn't hold.
    Nothing was written."""


class ProgressBehind(RuntimeError):
    """Another save for later landed since the page was made. Nothing was stored."""


class DraftFrozen(RuntimeError):
    """Some homework from the draft is saved, so its screenshots stay as they are: no image is
    added or removed, no Read starts, and the review can't be answered "Not hers". Nothing was
    written."""


class Closing(NamedTuple):
    """What a closing call's transaction committed: the version after, whether it closed the
    draft, and whether any acceptance names the draft, read in that same transaction. No
    draft operation deletes an acceptance, and no save lands in a closed draft, so the last is
    what the review's outcome is."""

    version: int
    closed: bool
    saved: bool


class StoredAnswer(NamedTuple):
    """One answer the review keeps: its kind, its subject, its value and its correction digest."""

    kind: str
    subject: str
    value: str
    digest: str


class AnswerToKeep(NamedTuple):
    """An answer a press posts, with the piece names it is about, so that an answer for pieces
    another save accepted since the page was made is dropped rather than stored."""

    kind: str
    subject: str
    value: str
    digest: str
    names: tuple[str, ...] = ()


class AnswersKept(NamedTuple):
    """What keeping a press's answers did: the version after, how many answers it stored, how
    many it dropped as already saved, and how many it withdrew."""

    version: int
    stored: int
    dropped: int
    withdrawn: int = 0


class SavedForLater(NamedTuple):
    """What Save review for later did: the answers kept, and whether the progress was stored
    against the page's revision, with the revision after."""

    answers: AnswersKept
    progress_saved: bool
    progress_revision: int


class ReviewAnswers(NamedTuple):
    """A draft's kept answers and marks, as one read saw them."""

    answers: tuple[StoredAnswer, ...]
    marks: frozenset[str]


class Carry(NamedTuple):
    """What a change of readings does to the review: the answers it clears, by kind and
    subject, the piece names it marks, and the row keys it sets aside or asks about again."""

    delete: frozenset[tuple[str, str]] = frozenset()
    marks: frozenset[str] = frozenset()
    aside_insert: frozenset[str] = frozenset()
    aside_delete: frozenset[str] = frozenset()


CarryFunction = Callable[
    [
        Sequence["StoredReading"],
        Sequence["StoredReading"],
        Mapping[tuple[str, str], str],
        frozenset[str],
    ],
    Carry,
]
"""A pure function from the readings before and after a change, the kept answers and the rows
set aside, to what the change clears, marks and carries."""

PiecesFunction = Callable[
    [Sequence["StoredReading"], Mapping[str, str]], Mapping[str, frozenset[str]]
]
"""A pure function from the readings and the kept split answers (row key to value) to each row
key's piece names."""


ACCEPTED_KEYS: Final = (
    "SELECT accepted FROM grade_acceptances WHERE student_id = ? AND kind = ? AND source_key = ?"
)

TABLES: Final = (
    """
    CREATE TABLE IF NOT EXISTS import_drafts (
        draft_id TEXT PRIMARY KEY,
        student_id TEXT NOT NULL,
        kind TEXT NOT NULL CHECK (kind IN ('homework_screenshot', 'grade_screenshot')),
        made_by TEXT NOT NULL CHECK (made_by IN ('parent', 'household')),
        creation_token TEXT NOT NULL UNIQUE,
        created_at TEXT NOT NULL,
        expires_at TEXT NOT NULL,
        version INTEGER NOT NULL CHECK (version >= 1),
        page_key BLOB NOT NULL,
        attempt_id TEXT,
        attempt_ends REAL,
        progress_revision INTEGER NOT NULL CHECK (progress_revision >= 0),
        closed_at TEXT,
        CHECK ((attempt_id IS NULL) = (attempt_ends IS NULL))
    )
    """,
    f"""
    CREATE TABLE IF NOT EXISTS import_draft_images (
        image_id TEXT PRIMARY KEY,
        draft_id TEXT NOT NULL,
        position INTEGER NOT NULL,
        sha256 TEXT NOT NULL,
        png BLOB NOT NULL,
        width INTEGER NOT NULL CHECK (width >= 1),
        height INTEGER NOT NULL CHECK (height >= 1),
        size INTEGER NOT NULL CHECK (size >= 1),
        added_at TEXT NOT NULL,
        attempt_id TEXT,
        reading TEXT,
        reading_kind TEXT,
        prompt_version TEXT,
        schema_version TEXT,
        failure TEXT CHECK (failure IN ({", ".join(f"'{kind}'" for kind in FAILURES)})),
        seconds REAL,
        input_tokens INTEGER,
        output_tokens INTEGER,
        UNIQUE (draft_id, sha256),
        CHECK (reading IS NULL OR failure IS NULL),
        CHECK ((reading IS NULL) = (reading_kind IS NULL)),
        CHECK ((reading IS NULL) = (prompt_version IS NULL)),
        CHECK ((reading IS NULL) = (schema_version IS NULL))
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS import_draft_discards (
        draft_id TEXT NOT NULL,
        row_key TEXT NOT NULL,
        discarded_at TEXT NOT NULL,
        PRIMARY KEY (draft_id, row_key)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS import_draft_progress (
        draft_id TEXT PRIMARY KEY,
        progress TEXT NOT NULL,
        saved_at TEXT NOT NULL
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS import_draft_answers (
        draft_id TEXT NOT NULL,
        kind TEXT NOT NULL,
        subject TEXT NOT NULL,
        value TEXT NOT NULL,
        digest TEXT NOT NULL,
        PRIMARY KEY (draft_id, kind, subject)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS import_draft_marks (
        draft_id TEXT NOT NULL,
        subject TEXT NOT NULL,
        PRIMARY KEY (draft_id, subject)
    )
    """,
)

DRAFT_OF: Final = "SELECT * FROM import_drafts WHERE draft_id = ? AND student_id = ?"
OPEN_DRAFTS: Final = (
    "SELECT d.draft_id, d.kind, d.created_at, d.expires_at, "
    "(SELECT COUNT(*) FROM import_draft_images AS i WHERE i.draft_id = d.draft_id) AS images "
    "FROM import_drafts AS d WHERE d.student_id = ? AND d.closed_at IS NULL "
    "ORDER BY d.created_at, d.draft_id"
)
"""Her drafts not closed, each with its image count, read without a rows function, an
acceptance record or a reading."""
IMAGES_OF: Final = (
    "SELECT image_id, position, sha256, width, height, size, added_at, attempt_id, reading, "
    "reading_kind, prompt_version, schema_version, failure FROM import_draft_images "
    "WHERE draft_id = ? ORDER BY position"
)
"""A draft's image rows without their PNGs, which only ``image_png`` reads."""
IMAGE_OF: Final = (
    "SELECT image_id, sha256, attempt_id, reading FROM import_draft_images "
    "WHERE draft_id = ? AND image_id = ?"
)
"""What a result is checked against: one image row's id, hash, attempt and reading."""
RAISE_VERSION: Final = "UPDATE import_drafts SET version = version + 1 WHERE draft_id = ?"
ANSWERS_OF: Final = (
    "SELECT kind, subject, value, digest FROM import_draft_answers WHERE draft_id = ?"
)
KEEP_ANSWER: Final = (
    "INSERT INTO import_draft_answers (draft_id, kind, subject, value, digest) "
    "VALUES (?, ?, ?, ?, ?) ON CONFLICT (draft_id, kind, subject) DO UPDATE SET "
    "value = excluded.value, digest = excluded.digest"
)
"""One answer stored, or its value and digest replaced."""
DELETE_DRAFT: Final = (
    "DELETE FROM import_draft_images WHERE draft_id = ?",
    "DELETE FROM import_draft_discards WHERE draft_id = ?",
    "DELETE FROM import_draft_progress WHERE draft_id = ?",
    "DELETE FROM import_draft_answers WHERE draft_id = ?",
    "DELETE FROM import_draft_marks WHERE draft_id = ?",
    "DELETE FROM import_drafts WHERE draft_id = ?",
)
"""Everything of one draft, in the order the sweep deletes it at expiry."""


def new_creation_token() -> str:
    """A one-time id for an Add screenshots form, so a resent first upload finds its draft."""
    return secrets.token_hex(16)


class ImportDraftsStore:
    """SQLite-backed import drafts, shared across worker threads behind a lock."""

    name = "import_drafts"
    retention_policy = (
        "Keep a review's screenshots, what was read from them, the answers given on it, and "
        "any progress saved for later available until the review finishes or is discarded, "
        "and never past 48 hours from when it was started. Unavailable and removed are two "
        "steps. From the instant a review is 48 hours old, nothing of it can be read. When a "
        "review finishes or is discarded, its screenshots, readings, answers and progress are "
        "removed from the file at once, or by the next hourly sweep or the next start. "
        "Whatever of a review is left at 48 hours, what is left of a closed review included "
        "(its ids, times, page key and the keys of rows set aside), is removed from the file "
        "by the first sweep after those 48 hours, or at the next start if Blossom was stopped. "
        "Removal from the file is all that is promised: a backup or a disk snapshot taken "
        "while the review was open may still hold earlier copies. What a save kept lives in "
        "the record under its own rules, never here."
    )

    def __init__(
        self,
        connection: sqlite3.Connection,
        clock: Clock,
        monotonic: Callable[[], float] = time.monotonic,
        *,
        rows: Mapping[str, RowsFunction],
        carries: Mapping[str, CarryFunction] | None = None,
        pieces: Mapping[str, PiecesFunction] | None = None,
    ) -> None:
        self._connection = connection
        self._connection.row_factory = sqlite3.Row
        self._connection.text_factory = text_or_named_refusal
        self._clock = clock
        self._monotonic = monotonic
        self._rows = dict(rows)
        self._carries = dict(carries or {})
        self._pieces = dict(pieces or {})
        unknown = (set(self._rows) | set(self._carries) | set(self._pieces)) - KINDS
        if unknown:
            msg = f"no draft kind is named {sorted(unknown)}"
            raise ValueError(msg)
        self._lock = threading.Lock()
        self._connection.execute("BEGIN IMMEDIATE")
        try:
            for table in TABLES:
                self._connection.execute(table)
            self._connection.commit()
        except BaseException:
            self._connection.rollback()
            raise

    @classmethod
    def open(
        cls,
        path: Path,
        clock: Clock,
        monotonic: Callable[[], float] = time.monotonic,
        *,
        rows: Mapping[str, RowsFunction],
        carries: Mapping[str, CarryFunction] | None = None,
        pieces: Mapping[str, PiecesFunction] | None = None,
    ) -> "ImportDraftsStore":
        """Open the household's file, refusing the places the saved-state store refuses, with
        deleted pages overwritten and SQLite's temporary storage held in memory."""
        safe = refuse_unsafe_path(path)
        safe.parent.mkdir(parents=True, exist_ok=True)
        connection = sqlite3.connect(safe, check_same_thread=False)
        connection.execute("PRAGMA secure_delete=ON")
        connection.execute("PRAGMA temp_store=MEMORY")
        return cls(connection, clock, monotonic, rows=rows, carries=carries, pieces=pieces)

    def close(self, wait: float = STORE_WAIT_SECONDS) -> None:
        """Close the underlying connection once no other call holds the store."""
        if not self._lock.acquire(timeout=max(0.0, wait)):
            raise StoreBusy
        try:
            self._connection.close()
        finally:
            self._lock.release()

    # ------------------------------------------------------------------ uploads

    def create(
        self,
        student_id: str,
        *,
        kind: str,
        made_by: str,
        creation_token: str,
        images: Sequence[NewImage],
        wait: float = STORE_WAIT_SECONDS,
    ) -> Added | None:
        """A first upload: the draft its token names, made now when there is none. A resent
        upload adds to the same draft; an upload with no image makes none and returns None."""
        if kind not in KINDS:
            msg = "no draft kind has that name"
            raise ValueError(msg)
        if kind not in self._rows:
            raise KindNotWired(kind)
        if made_by not in MADE_BY:
            msg = "a draft is made by a parent or the household"
            raise ValueError(msg)
        if not TOKEN_SHAPE.fullmatch(creation_token):
            msg = "a creation token is 32 hex characters"
            raise ValueError(msg)
        hashed = digested(images)
        with self._writing(wait) as now:
            made = self._connection.execute(
                "SELECT draft_id, student_id, kind FROM import_drafts WHERE creation_token = ?",
                (creation_token,),
            ).fetchone()
            if made is not None:
                if made["student_id"] != student_id or made["kind"] != kind:
                    raise DraftNotFound
                return self._add(student_id, str(made["draft_id"]), hashed, now, raise_version=True)
            if not hashed:
                return None
            draft_id = secrets.token_hex(16)
            self._connection.execute(
                "INSERT INTO import_drafts (draft_id, student_id, kind, made_by, creation_token, "
                "created_at, expires_at, version, page_key, progress_revision) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, 1, ?, 0)",
                (
                    draft_id,
                    student_id,
                    kind,
                    made_by,
                    creation_token,
                    instant(now),
                    instant(now + DRAFT_LIFETIME),
                    secrets.token_bytes(32),
                ),
            )
            return self._add(student_id, draft_id, hashed, now, raise_version=False)

    def add_images(
        self,
        student_id: str,
        draft_id: str,
        images: Sequence[NewImage],
        wait: float = STORE_WAIT_SECONDS,
    ) -> Added:
        """Add an upload's images to an open draft, with no version comparison. Duplicates
        are set aside; if the rest would make more than ten present, nothing is added."""
        hashed = digested(images)
        with self._writing(wait) as now:
            return self._add(student_id, draft_id, hashed, now, raise_version=True)

    def _add(
        self,
        student_id: str,
        draft_id: str,
        images: Sequence[tuple[str, NewImage]],
        now: datetime,
        *,
        raise_version: bool,
    ) -> Added:
        """Inside a write: the draft checks, the duplicates, the ten-present rule, the inserts,
        for images hashed before it began. A draft made in this transaction starts at version
        1 with its first images."""
        draft = self._open_draft(student_id, draft_id, now)
        if self._frozen(draft):
            raise DraftFrozen
        held = {
            str(row["sha256"]): int(row["position"])
            for row in self._connection.execute(IMAGES_OF, (draft_id,))
        }
        fresh: list[tuple[str, NewImage]] = []
        duplicates: list[int] = []
        for place, (digest, item) in enumerate(images):
            if digest in held or any(digest == seen for seen, _ in fresh):
                duplicates.append(place)
            else:
                fresh.append((digest, item))
        free = MOST_IMAGES_PRESENT - len(held)
        if len(fresh) > free:
            raise TooManyImages(free)
        if not fresh:
            return Added(draft_id, int(draft["version"]), (), tuple(duplicates), free)
        position = max(held.values(), default=-1)
        added = []
        for digest, item in fresh:
            position += 1
            image_id = secrets.token_hex(16)
            self._connection.execute(
                "INSERT INTO import_draft_images (image_id, draft_id, position, sha256, png, "
                "width, height, size, added_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    image_id,
                    draft_id,
                    position,
                    digest,
                    item.png,
                    item.width,
                    item.height,
                    len(item.png),
                    instant(now),
                ),
            )
            added.append(image_id)
        if raise_version:
            self._connection.execute(RAISE_VERSION, (draft_id,))
        return Added(
            draft_id,
            self._version(draft_id),
            tuple(added),
            tuple(duplicates),
            free - len(fresh),
        )

    def remove_image(
        self, student_id: str, draft_id: str, image_id: str, wait: float = STORE_WAIT_SECONDS
    ) -> int:
        """Delete one image and its reading, with no version comparison, even a reading that
        arrived since the page opened; return the raised version. What the change does to the
        review's answers is applied in the same transaction. A draft this finishes closes."""
        with self._writing(wait) as now:
            draft = self._open_draft(student_id, draft_id, now)
            if self._frozen(draft):
                raise DraftFrozen
            before = self._worked_out(draft).readings
            removed = self._connection.execute(
                "DELETE FROM import_draft_images WHERE draft_id = ? AND image_id = ?",
                (draft_id, image_id),
            ).rowcount
            if removed != 1:
                raise UnknownImage
            self._connection.execute(RAISE_VERSION, (draft_id,))
            self._carried(draft, before)
            self._close_if_finished(draft, now)
            return self._version(draft_id)

    def image_png(
        self, student_id: str, draft_id: str, image_id: str, wait: float = STORE_WAIT_SECONDS
    ) -> bytes:
        """The prepared PNG of one image of an unexpired draft neither closed nor finished: the
        bytes shown and sent."""
        with self._reading(wait) as now:
            self._open_draft(student_id, draft_id, now)
            row = self._connection.execute(
                "SELECT png FROM import_draft_images WHERE draft_id = ? AND image_id = ?",
                (draft_id, image_id),
            ).fetchone()
            if row is None:
                raise UnknownImage
            return bytes(row["png"])

    # ------------------------------------------------------------------ reading

    def start_attempt(
        self,
        student_id: str,
        draft_id: str,
        image_ids: Collection[str],
        wait: float = STORE_WAIT_SECONDS,
    ) -> Attempt | None:
        """Start a Read attempt over the unread images among ``image_ids``, failed ones
        included, with no version comparison. None when none of them is unread. An id that
        isn't an image of the draft is passed over."""
        with self._writing(wait) as now:
            draft = self._open_draft(student_id, draft_id, now)
            if self._frozen(draft):
                raise DraftFrozen
            current = self._current_ends(draft)
            if current is not None:
                raise AttemptUnderWay(current)
            asked = set(image_ids)
            covered = tuple(
                str(row["image_id"])
                for row in self._connection.execute(IMAGES_OF, (draft_id,))
                if str(row["image_id"]) in asked and row["reading"] is None
            )
            if not covered:
                return None
            attempt = Attempt(
                secrets.token_hex(16), self._monotonic() + READ_DEADLINE_SECONDS, covered
            )
            self._connection.execute(
                "UPDATE import_drafts SET attempt_id = ?, attempt_ends = ? WHERE draft_id = ?",
                (attempt.attempt_id, attempt.ends, draft_id),
            )
            self._connection.execute(
                "UPDATE import_draft_images SET attempt_id = NULL WHERE draft_id = ?", (draft_id,)
            )
            self._connection.executemany(
                "UPDATE import_draft_images SET attempt_id = ? WHERE image_id = ?",
                [(attempt.attempt_id, image_id) for image_id in covered],
            )
            return attempt

    def store_reading(
        self,
        student_id: str,
        draft_id: str,
        *,
        attempt_id: str,
        image_id: str,
        sha256: str,
        reading: UploadedReading,
        timing: ReadTiming,
        wait: float = STORE_WAIT_SECONDS,
    ) -> bool:
        """Store a validated reading for the image its call was made for, only while the
        attempt is current and covers that image, and the draft is unexpired, open and not
        finished; whether it was stored. A stored reading raises the version."""
        with self._writing(wait) as now:
            draft = self._result_target(student_id, draft_id, attempt_id, image_id, sha256, now)
            if draft is None:
                log_dropped("a reading", timing)
                return False
            before = self._worked_out(draft).readings
            self._connection.execute(
                "UPDATE import_draft_images SET reading = ?, reading_kind = ?, "
                "prompt_version = ?, schema_version = ?, failure = NULL, seconds = ?, "
                "input_tokens = ?, output_tokens = ? WHERE image_id = ?",
                (
                    reading.body,
                    reading.kind,
                    reading.prompt_version,
                    reading.schema_version,
                    timing.seconds,
                    timing.input_tokens,
                    timing.output_tokens,
                    image_id,
                ),
            )
            self._connection.execute(RAISE_VERSION, (draft_id,))
            self._carried(draft, before)
            self._close_if_finished(draft, now)
            return True

    def store_failure(
        self,
        student_id: str,
        draft_id: str,
        *,
        attempt_id: str,
        image_id: str,
        sha256: str,
        failure: str,
        timing: ReadTiming,
        wait: float = STORE_WAIT_SECONDS,
    ) -> bool:
        """Record a failed read under the same conditions as a reading; whether it was kept.
        The image stays open, and the version doesn't change."""
        if failure not in FAILURES:
            msg = "a failure is one of the closed set"
            raise ValueError(msg)
        with self._writing(wait) as now:
            draft = self._result_target(student_id, draft_id, attempt_id, image_id, sha256, now)
            if draft is None:
                log_dropped("a failure", timing)
                return False
            self._connection.execute(
                "UPDATE import_draft_images SET failure = ?, seconds = ?, input_tokens = ?, "
                "output_tokens = ? WHERE image_id = ?",
                (failure, timing.seconds, timing.input_tokens, timing.output_tokens, image_id),
            )
            return True

    def end_attempt(
        self, student_id: str, draft_id: str, attempt_id: str, wait: float = STORE_WAIT_SECONDS
    ) -> bool:
        """End a Read attempt whose calls have all ended: while it is still the draft's current
        attempt, its deadline moves to now, so another press can start one at once. Its id stays
        on the draft and its images, and a result for it is dropped from now on. Whether it did;
        another attempt, one already past its deadline, or a draft not hers, closed or expired
        changes nothing."""
        with self._writing(wait) as now:
            draft = self._connection.execute(DRAFT_OF, (draft_id, student_id)).fetchone()
            if (
                draft is None
                or now >= moment(draft["expires_at"])
                or draft["closed_at"] is not None
                or draft["attempt_id"] != attempt_id
                or self._current_ends(draft) is None
            ):
                return False
            self._connection.execute(
                "UPDATE import_drafts SET attempt_ends = ? WHERE draft_id = ? AND attempt_id = ?",
                (self._monotonic(), draft_id, attempt_id),
            )
            return True

    def clear_attempts(self, wait: float = STORE_WAIT_SECONDS) -> int:
        """End every attempt, for a process starting before it serves; how many drafts had one."""
        with self._writing(wait):
            cleared = self._connection.execute(
                "UPDATE import_drafts SET attempt_id = NULL, attempt_ends = NULL "
                "WHERE attempt_id IS NOT NULL"
            ).rowcount
            self._connection.execute(
                "UPDATE import_draft_images SET attempt_id = NULL WHERE attempt_id IS NOT NULL"
            )
            return int(cleared)

    def _result_target(
        self,
        student_id: str,
        draft_id: str,
        attempt_id: str,
        image_id: str,
        sha256: str,
        now: datetime,
    ) -> sqlite3.Row | None:
        """Inside a write: the draft a result may be stored in, or None when it is dropped."""
        draft = self._connection.execute(DRAFT_OF, (draft_id, student_id)).fetchone()
        if draft is None or now >= moment(draft["expires_at"]) or draft["closed_at"] is not None:
            return None
        if draft["attempt_id"] != attempt_id or self._current_ends(draft) is None:
            return None
        row = self._connection.execute(
            IMAGE_OF,
            (draft_id, image_id),
        ).fetchone()
        if row is None or row["sha256"] != sha256 or row["attempt_id"] != attempt_id:
            return None
        if row["reading"] is not None or self._worked_out(draft).finished or self._frozen(draft):
            return None
        return cast("sqlite3.Row", draft)

    # ------------------------------------------------------------------ review

    def snapshot(
        self, student_id: str, draft_id: str, wait: float = STORE_WAIT_SECONDS
    ) -> DraftSnapshot:
        """The draft as one read sees it, with every row's state from the rows function and
        the acceptance records read in the same read."""
        with self._reading(wait) as now:
            draft = self._present(student_id, draft_id, now)
            return self._snapshot(draft)

    def save_progress(
        self,
        student_id: str,
        draft_id: str,
        *,
        revision: int,
        progress: str,
        wait: float = STORE_WAIT_SECONDS,
    ) -> int:
        """Save a review's progress for later, compared and set against its revision; the new
        revision. It never raises the version, extends expiry, saves or discards a row."""
        with self._writing(wait) as now:
            draft = self._open_draft(student_id, draft_id, now)
            if int(draft["progress_revision"]) != revision:
                raise ProgressBehind
            self._connection.execute(
                "INSERT INTO import_draft_progress (draft_id, progress, saved_at) "
                "VALUES (?, ?, ?) ON CONFLICT (draft_id) DO UPDATE SET "
                "progress = excluded.progress, saved_at = excluded.saved_at",
                (draft_id, progress, instant(now)),
            )
            self._connection.execute(
                "UPDATE import_drafts SET progress_revision = progress_revision + 1 "
                "WHERE draft_id = ?",
                (draft_id,),
            )
            return revision + 1

    def review_answers(
        self, student_id: str, draft_id: str, wait: float = STORE_WAIT_SECONDS
    ) -> ReviewAnswers:
        """The answers and marks an unexpired draft keeps, in one read."""
        with self._reading(wait) as now:
            self._present(student_id, draft_id, now)
            answers = tuple(
                StoredAnswer(str(kind), str(subject), str(value), str(digest))
                for kind, subject, value, digest in self._connection.execute(
                    ANSWERS_OF, (draft_id,)
                )
            )
            marks = frozenset(
                str(subject)
                for (subject,) in self._connection.execute(
                    "SELECT subject FROM import_draft_marks WHERE draft_id = ?", (draft_id,)
                )
            )
        return ReviewAnswers(tuple(sorted(answers)), marks)

    def keep_answers(
        self,
        student_id: str,
        draft_id: str,
        *,
        version: int,
        answers: Sequence[AnswerToKeep],
        withdrawn: Sequence[tuple[str, str]] = (),
        wait: float = STORE_WAIT_SECONDS,
    ) -> AnswersKept:
        """Keep a press's answers at the page's version, all or none, in one writer transaction:
        an answer for pieces another save accepted is dropped, an answer equal to the one kept
        writes nothing, each answer the page asked for and the press left blank (``withdrawn``,
        by kind and subject) is deleted, and the version is raised once when any answer
        changed."""
        with self._writing(wait) as now:
            draft = self._at_version(student_id, draft_id, version, now)
            return self._keep_answers_locked(draft, answers, withdrawn)

    def save_for_later(
        self,
        student_id: str,
        draft_id: str,
        *,
        version: int,
        progress_revision: int,
        answers: Sequence[AnswerToKeep],
        progress: str,
        withdrawn: Sequence[tuple[str, str]] = (),
        wait: float = STORE_WAIT_SECONDS,
    ) -> SavedForLater:
        """Save review for later in one writer transaction: the answers as ``keep_answers``
        keeps them, then the progress compared and set against the page's revision. A page
        behind keeps its answers and stores no progress."""
        with self._writing(wait) as now:
            draft = self._at_version(student_id, draft_id, version, now)
            kept = self._keep_answers_locked(draft, answers, withdrawn)
            revision = int(draft["progress_revision"])
            if revision != progress_revision:
                return SavedForLater(kept, False, revision)
            self._connection.execute(
                "INSERT INTO import_draft_progress (draft_id, progress, saved_at) "
                "VALUES (?, ?, ?) ON CONFLICT (draft_id) DO UPDATE SET "
                "progress = excluded.progress, saved_at = excluded.saved_at",
                (draft_id, progress, instant(now)),
            )
            self._connection.execute(
                "UPDATE import_drafts SET progress_revision = progress_revision + 1 "
                "WHERE draft_id = ?",
                (draft_id,),
            )
            return SavedForLater(kept, True, revision + 1)

    def _keep_answers_locked(
        self,
        draft: sqlite3.Row,
        answers: Sequence[AnswerToKeep],
        withdrawn: Sequence[tuple[str, str]] = (),
    ) -> AnswersKept:
        """Inside a write at the page's version: the dropped, the unchanged, the stored and the
        withdrawn answers, with one version raise when any changed."""
        draft_id = str(draft["draft_id"])
        saved = self._worked_out(draft).saved
        kept = {
            (str(kind), str(subject)): (str(value), str(digest))
            for kind, subject, value, digest in self._connection.execute(ANSWERS_OF, (draft_id,))
        }
        changed: dict[tuple[str, str], tuple[str, str]] = {}
        dropped = 0
        for answer in answers:
            if answer.names and all(name in saved for name in answer.names):
                dropped += 1
                continue
            if kept.get((answer.kind, answer.subject)) != (answer.value, answer.digest):
                changed[(answer.kind, answer.subject)] = (answer.value, answer.digest)
        for (kind, subject), (value, digest) in sorted(changed.items()):
            self._connection.execute(KEEP_ANSWER, (draft_id, kind, subject, value, digest))
        gone = sorted({key for key in withdrawn if key in kept and key not in changed})
        self._connection.executemany(
            "DELETE FROM import_draft_answers WHERE draft_id = ? AND kind = ? AND subject = ?",
            [(draft_id, kind, subject) for kind, subject in gone],
        )
        if changed or gone:
            self._connection.execute(RAISE_VERSION, (draft_id,))
        return AnswersKept(self._version(draft_id), len(changed), dropped, len(gone))

    def discard_rest(
        self,
        student_id: str,
        draft_id: str,
        *,
        version: int,
        keys: Collection[str],
        wait: float = STORE_WAIT_SECONDS,
    ) -> Closing:
        """Discard the open rows a page showed and remove the images with no reading, at the
        page's version; what the transaction committed. Rows already saved are skipped. A
        draft this finishes closes in the same transaction."""
        with self._writing(wait) as now:
            draft = self._at_version(student_id, draft_id, version, now)
            worked = self._worked_out(draft)
            if not set(keys) <= {row.key for row in worked.rows}:
                raise ReviewChanged
            self._connection.executemany(
                "INSERT OR IGNORE INTO import_draft_discards (draft_id, row_key, discarded_at) "
                "VALUES (?, ?, ?)",
                [(draft_id, key, instant(now)) for key in keys if key not in worked.saved],
            )
            removed = self._connection.execute(
                "DELETE FROM import_draft_images WHERE draft_id = ? AND reading IS NULL",
                (draft_id,),
            ).rowcount
            if removed:
                self._connection.execute(RAISE_VERSION, (draft_id,))
            closed = self._close_if_finished(draft, now)
            return Closing(self._version(draft_id), closed, self._accepted(draft))

    def set_aside(
        self,
        student_id: str,
        draft_id: str,
        *,
        version: int,
        keys: Collection[str],
        wait: float = STORE_WAIT_SECONDS,
    ) -> Closing:
        """Set a group's rows or items aside at the page's version, all or none: each key is
        discarded, a key already saved is skipped, and the version is raised once. When nothing
        is left open, the draft closes in the same transaction. What the transaction
        committed."""
        with self._writing(wait) as now:
            draft = self._at_version(student_id, draft_id, version, now)
            worked = self._worked_out(draft)
            present = {row.key for row in worked.rows}
            if not keys or not set(keys) <= present:
                raise ReviewChanged
            self._connection.executemany(
                "INSERT OR IGNORE INTO import_draft_discards (draft_id, row_key, discarded_at) "
                "VALUES (?, ?, ?)",
                [
                    (draft_id, key, instant(now))
                    for key in sorted(set(keys))
                    if key not in worked.saved
                ],
            )
            self._connection.execute(RAISE_VERSION, (draft_id,))
            closed = self._close_if_finished(draft, now)
            return Closing(self._version(draft_id), closed, self._accepted(draft))

    def discard_review(
        self,
        student_id: str,
        draft_id: str,
        *,
        version: int,
        refuse_if_saved: bool = False,
        wait: float = STORE_WAIT_SECONDS,
    ) -> Closing:
        """Discard the whole review, as "Discard the review" and "Not hers" do, at the page's
        version: every open row discarded, every image, reading and saved progress deleted,
        and the draft closed; what the transaction committed. No acceptance is written. For
        "Not hers", ``refuse_if_saved``: once any homework from it is saved, as this
        transaction reads it, ``DraftFrozen`` and nothing is written, whatever page asked."""
        with self._writing(wait) as now:
            if refuse_if_saved and self._frozen(self._open_draft(student_id, draft_id, now)):
                raise DraftFrozen
            draft = self._at_version(student_id, draft_id, version, now)
            worked = self._worked_out(draft)
            self._connection.executemany(
                "INSERT OR IGNORE INTO import_draft_discards (draft_id, row_key, discarded_at) "
                "VALUES (?, ?, ?)",
                [(draft_id, row.key, instant(now)) for row in worked.rows if row.state == "open"],
            )
            self._close(draft_id, now)
            return Closing(self._version(draft_id), True, self._accepted(draft))

    def clean_up(self, student_id: str, draft_id: str, wait: float = STORE_WAIT_SECONDS) -> bool:
        """Close a finished draft: its images, readings and saved progress deleted and the
        version raised. Whether it closed it. Nothing is written outside the draft's tables."""
        with self._writing(wait) as now:
            draft = self._present(student_id, draft_id, now)
            if draft["closed_at"] is not None:
                return False
            return self._close_if_finished(draft, now)

    def sweep(self, wait: float = STORE_WAIT_SECONDS) -> Swept:
        """Delete every draft past its expiry, and close every finished one, one draft per
        transaction, finished worked out inside each. This is where an expired draft, refused
        by every call since the instant it expired, is removed from the file.

        A draft whose rows or acceptance records can't be worked out is left open, logged by
        the kind of its failure alone, and the sweep goes on to the next. A failed deletion,
        or a failure of the file itself, still ends it."""
        with self._reading(wait):
            drafts = [
                str(row["draft_id"])
                for row in self._connection.execute(
                    "SELECT draft_id FROM import_drafts ORDER BY created_at, draft_id"
                )
            ]
        expired = closed = passed_over = 0
        for draft_id in drafts:
            unworkable = ""
            with self._writing(wait) as now:
                draft = self._connection.execute(
                    "SELECT * FROM import_drafts WHERE draft_id = ?", (draft_id,)
                ).fetchone()
                if draft is None:
                    continue
                if now >= moment(draft["expires_at"]):
                    for statement in DELETE_DRAFT:
                        self._connection.execute(statement, (draft_id,))
                    expired += 1
                elif draft["closed_at"] is None:
                    finished = self._finished_or_failure(draft)
                    if isinstance(finished, str):
                        unworkable = finished
                    elif finished:
                        self._close(draft_id, now)
                        closed += 1
            if unworkable:
                passed_over += 1
                logger.warning(
                    "the import draft sweep left a draft it couldn't work out open: %s",
                    unworkable,
                )
        if expired or closed:
            logger.info("the import draft sweep deleted %d and closed %d", expired, closed)
        return Swept(expired, closed, passed_over)

    # ------------------------------------------------------------------ inside a transaction

    def _present(self, student_id: str, draft_id: str, now: datetime) -> sqlite3.Row:
        """Checks 1 and 2: the draft exists, is hers, and hasn't expired. An expired draft is
        refused from that instant, whether or not a sweep has removed it yet."""
        draft = self._connection.execute(DRAFT_OF, (draft_id, student_id)).fetchone()
        if draft is None:
            raise DraftNotFound
        if now >= moment(draft["expires_at"]):
            raise DraftExpired
        return cast("sqlite3.Row", draft)

    def _open_draft(self, student_id: str, draft_id: str, now: datetime) -> sqlite3.Row:
        """Checks 1 and 2, and the draft is neither closed nor finished."""
        draft = self._present(student_id, draft_id, now)
        if draft["closed_at"] is not None or self._worked_out(draft).finished:
            raise DraftClosed
        return draft

    def _at_version(
        self, student_id: str, draft_id: str, version: int, now: datetime
    ) -> sqlite3.Row:
        """Checks 1, 2 and 3 for a discard: present, not closed, at the page's version."""
        draft = self._open_draft(student_id, draft_id, now)
        if int(draft["version"]) != version:
            raise ReviewChanged
        return draft

    def _current_ends(self, draft: sqlite3.Row) -> float | None:
        """The draft's attempt deadline while it is current on the monotonic clock."""
        ends = draft["attempt_ends"]
        if draft["attempt_id"] is None or ends is None or float(ends) <= self._monotonic():
            return None
        return float(ends)

    def _version(self, draft_id: str) -> int:
        row = self._connection.execute(
            "SELECT version FROM import_drafts WHERE draft_id = ?", (draft_id,)
        ).fetchone()
        return int(row["version"])

    def _worked_out(self, draft: sqlite3.Row) -> "_Worked":
        """The draft's images, readings and rows, each row saved, discarded or open, and
        whether the draft is finished, from what this transaction reads."""
        draft_id = str(draft["draft_id"])
        images = self._connection.execute(IMAGES_OF, (draft_id,)).fetchall()
        readings = tuple(
            sorted(
                (
                    StoredReading(
                        sha256=str(row["sha256"]),
                        kind=str(row["reading_kind"]),
                        prompt_version=str(row["prompt_version"]),
                        schema_version=str(row["schema_version"]),
                        body=str(row["reading"]),
                    )
                    for row in images
                    if row["reading"] is not None
                ),
                key=lambda item: item.sha256,
            )
        )
        saved: set[str] = set()
        for (accepted,) in self._connection.execute(
            ACCEPTED_KEYS, (draft["student_id"], draft["kind"], draft_id)
        ):
            saved.update(accepted_keys(accepted))
        saved |= self._whole_rows(draft, readings, frozenset(saved))
        discards = {
            str(key)
            for (key,) in self._connection.execute(
                "SELECT row_key FROM import_draft_discards WHERE draft_id = ?", (draft_id,)
            )
        }
        function = self._rows.get(str(draft["kind"]))
        found: list[DraftRow] = []
        if function is not None and readings:
            seen: set[str] = set()
            for row in function(readings):
                if row.key not in seen:
                    seen.add(row.key)
                    found.append(row)
        rows = tuple(
            RowView(
                row.key,
                row.readable,
                "saved" if row.key in saved else "discarded" if row.key in discards else "open",
            )
            for row in found
        )
        unread = any(row["reading"] is None for row in images)
        finished = (
            function is not None
            and not unread
            and all(row.state != "open" for row in rows)
            and bool(saved or discards)
        )
        return _Worked(
            images, readings, rows, frozenset(saved), frozenset(discards - saved), finished
        )

    def _finished_or_failure(self, draft: sqlite3.Row) -> bool | str:
        """Whether the draft is finished, worked out in this transaction; or, when its rows
        function fails, or decoding its acceptance records or readings does, that failure's
        kind alone, text that isn't UTF-8 included. Working it out writes nothing. A failure
        of the file itself is raised."""
        kind = ""
        try:
            return self._worked_out(draft).finished
        except TextNotUtf8 as refused:
            kind = type(refused).__name__
        except sqlite3.Error:
            raise
        except Exception as error:
            kind = type(error).__name__
        return kind

    def _close_if_finished(self, draft: sqlite3.Row, now: datetime) -> bool:
        """Close the draft when this transaction finds it finished; whether it did."""
        current = self._connection.execute(
            "SELECT * FROM import_drafts WHERE draft_id = ?", (draft["draft_id"],)
        ).fetchone()
        if current["closed_at"] is not None or not self._worked_out(current).finished:
            return False
        self._close(str(draft["draft_id"]), now)
        return True

    def _accepted(self, draft: sqlite3.Row) -> bool:
        """Whether any acceptance names the draft, as this transaction reads it."""
        return (
            self._connection.execute(
                ACCEPTED_KEYS + " LIMIT 1", (draft["student_id"], draft["kind"], draft["draft_id"])
            ).fetchone()
            is not None
        )

    def _frozen(self, draft: sqlite3.Row) -> bool:
        """Whether a homework draft is named by an acceptance: once some homework from it is
        saved, its screenshots and readings stay as they are. A grade draft is never frozen."""
        return str(draft["kind"]) == "homework_screenshot" and self._accepted(draft)

    def _whole_rows(
        self, draft: sqlite3.Row, readings: Sequence[StoredReading], saved: frozenset[str]
    ) -> set[str]:
        """The row keys whose pieces the acceptances all name, for a kind with a pieces
        function: a row split into pieces is saved once every piece is."""
        function = self._pieces.get(str(draft["kind"]))
        if function is None or not any("|" in name for name in saved):
            return set()
        split = {
            str(subject): str(value)
            for subject, value in self._connection.execute(
                "SELECT subject, value FROM import_draft_answers "
                "WHERE draft_id = ? AND kind = 'split'",
                (draft["draft_id"],),
            )
        }
        return {key for key, names in function(readings, split).items() if names and names <= saved}

    def _carried(self, draft: sqlite3.Row, before: Sequence[StoredReading]) -> None:
        """Inside a change's write: what the change of readings does to the review's answers,
        marks and set-aside rows, by the kind's carry function."""
        function = self._carries.get(str(draft["kind"]))
        if function is None:
            return
        draft_id = str(draft["draft_id"])
        after = self._worked_out(draft).readings
        if tuple(after) == tuple(before):
            return
        answers = {
            (str(kind), str(subject)): str(value)
            for kind, subject, value, _ in self._connection.execute(ANSWERS_OF, (draft_id,))
        }
        aside = frozenset(
            str(key)
            for (key,) in self._connection.execute(
                "SELECT row_key FROM import_draft_discards WHERE draft_id = ?", (draft_id,)
            )
        )
        carry = function(before, after, answers, aside)
        self._connection.executemany(
            "DELETE FROM import_draft_answers WHERE draft_id = ? AND kind = ? AND subject = ?",
            [(draft_id, kind, subject) for kind, subject in sorted(carry.delete)],
        )
        self._connection.executemany(
            "INSERT OR IGNORE INTO import_draft_marks (draft_id, subject) VALUES (?, ?)",
            [(draft_id, subject) for subject in sorted(carry.marks)],
        )
        stamp = instant(self._clock.now())
        self._connection.executemany(
            "INSERT OR IGNORE INTO import_draft_discards (draft_id, row_key, discarded_at) "
            "VALUES (?, ?, ?)",
            [(draft_id, key, stamp) for key in sorted(carry.aside_insert)],
        )
        self._connection.executemany(
            "DELETE FROM import_draft_discards WHERE draft_id = ? AND row_key = ?",
            [(draft_id, key) for key in sorted(carry.aside_delete)],
        )

    def _close(self, draft_id: str, now: datetime) -> None:
        """Delete the draft's images, readings, saved progress, answers and marks, end its
        attempt, set ``closed_at`` and raise the version. Nothing outside the draft's tables is
        touched."""
        self._connection.execute("DELETE FROM import_draft_images WHERE draft_id = ?", (draft_id,))
        self._connection.execute(
            "DELETE FROM import_draft_progress WHERE draft_id = ?", (draft_id,)
        )
        self._connection.execute("DELETE FROM import_draft_answers WHERE draft_id = ?", (draft_id,))
        self._connection.execute("DELETE FROM import_draft_marks WHERE draft_id = ?", (draft_id,))
        self._connection.execute(
            "UPDATE import_drafts SET closed_at = ?, attempt_id = NULL, attempt_ends = NULL, "
            "version = version + 1 WHERE draft_id = ?",
            (instant(now), draft_id),
        )

    def _snapshot(self, draft: sqlite3.Row) -> DraftSnapshot:
        worked = self._worked_out(draft)
        progress = self._connection.execute(
            "SELECT progress FROM import_draft_progress WHERE draft_id = ?", (draft["draft_id"],)
        ).fetchone()
        ends = self._current_ends(draft)
        return DraftSnapshot(
            draft_id=str(draft["draft_id"]),
            kind=str(draft["kind"]),
            made_by=str(draft["made_by"]),
            version=int(draft["version"]),
            created_at=moment(draft["created_at"]),
            expires_at=moment(draft["expires_at"]),
            closed_at=None if draft["closed_at"] is None else moment(draft["closed_at"]),
            page_key=bytes(draft["page_key"]),
            progress_revision=int(draft["progress_revision"]),
            progress=None if progress is None else str(progress["progress"]),
            attempt_ends=ends,
            images=tuple(
                ImageState(
                    image_id=str(row["image_id"]),
                    position=int(row["position"]),
                    sha256=str(row["sha256"]),
                    width=int(row["width"]),
                    height=int(row["height"]),
                    size=int(row["size"]),
                    added_at=moment(row["added_at"]),
                    read=row["reading"] is not None,
                    failure=None if row["failure"] is None else str(row["failure"]),
                    covered=ends is not None and in_attempt(row, draft),
                    in_ended_attempt=ends is None and in_attempt(row, draft),
                )
                for row in worked.images
            ),
            readings=worked.readings,
            rows=worked.rows,
            saved=worked.saved,
            discarded=worked.discarded,
            finished=worked.finished,
            frozen=self._frozen(draft),
        )

    def open_drafts(
        self, student_id: str, wait: float = STORE_WAIT_SECONDS
    ) -> tuple[OpenDraft, ...]:
        """Her reviews still open, unexpired and not closed, in the order they were started. It
        works out no rows and reads no reading or acceptance record, so no draft's content can
        make the list fail."""
        with self._reading(wait) as now:
            return tuple(
                OpenDraft(
                    draft_id=str(row["draft_id"]),
                    kind=str(row["kind"]),
                    created_at=moment(row["created_at"]),
                    expires_at=moment(row["expires_at"]),
                    images=int(row["images"]),
                )
                for row in self._connection.execute(OPEN_DRAFTS, (student_id,))
                if now < moment(row["expires_at"])
            )

    # ------------------------------------------------------------------ sessions

    @contextmanager
    def _reading(self, wait: float) -> Iterator[datetime]:
        """One consistent read under the lock, with the read's instant on the real clock."""
        ends = time.monotonic() + max(0.0, wait)
        if not self._lock.acquire(timeout=max(0.0, wait)):
            raise StoreBusy
        try:
            self._wait_until(ends)
            self._connection.execute("BEGIN")
            try:
                yield self._clock.now()
            finally:
                self._connection.rollback()
        finally:
            self._lock.release()

    @contextmanager
    def _writing(self, wait: float) -> Iterator[datetime]:
        """One write under the lock, with the file's writer reserved before anything is read
        and the move's instant read once it is: committed whole when the move returns, rolled
        back whole when it raises. ``StoreBusy`` and ``WriterBusy`` begin nothing."""
        ends = time.monotonic() + max(0.0, wait)
        if not self._lock.acquire(timeout=max(0.0, wait)):
            raise StoreBusy
        try:
            self._wait_until(ends)
            try:
                self._connection.execute("BEGIN IMMEDIATE")
            except sqlite3.OperationalError as error:
                if error.sqlite_errorcode == sqlite3.SQLITE_BUSY:
                    raise WriterBusy from None
                raise
            try:
                yield self._clock.now()
                self._wait_until(ends)
                self._connection.commit()
            except BaseException:
                self._connection.rollback()
                raise
        finally:
            self._lock.release()

    def _wait_until(self, ends: float) -> None:
        """Let the file's busy handler wait until ``ends``, an instant on ``time.monotonic``."""
        milliseconds = max(0, int((ends - time.monotonic()) * 1000))
        self._connection.execute(f"PRAGMA busy_timeout={milliseconds}")


class _Worked(NamedTuple):
    images: list[sqlite3.Row]
    readings: tuple[StoredReading, ...]
    rows: tuple[RowView, ...]
    saved: frozenset[str]
    discarded: frozenset[str]
    finished: bool


def in_attempt(image: sqlite3.Row, draft: sqlite3.Row) -> bool:
    """Whether the image carries the draft's last attempt id."""
    return image["attempt_id"] is not None and image["attempt_id"] == draft["attempt_id"]


def digested(images: Sequence[NewImage]) -> list[tuple[str, NewImage]]:
    """Each image with the SHA-256 of its bytes, taken before any transaction opens."""
    return [(hashlib.sha256(item.png).hexdigest(), item) for item in images]


def instant(value: datetime) -> str:
    """An instant as stored: UTC, in ISO form, so stored instants compare as text and time."""
    return value.astimezone(UTC).isoformat()


def moment(text: object) -> datetime:
    """A stored instant read back."""
    return datetime.fromisoformat(str(text))


class AcceptanceUnreadable(ValueError):
    """An acceptance record whose pairs aren't each a row key and its target."""

    def __init__(self) -> None:
        super().__init__("an acceptance record's pairs can't be read")


def accepted_keys(accepted: str) -> list[str]:
    """The row keys an acceptance record's pairs name. Each pair must be a list of exactly a
    row key, as text, and its target: an assignment id as text, or ``null`` where a grade save
    keeps no target, as for a term or a category. Anything else refuses the whole record, so
    no malformed entry can mark a row saved."""
    pairs = json.loads(accepted)
    if not isinstance(pairs, list) or not all(
        isinstance(pair, list)
        and len(pair) == 2
        and isinstance(pair[0], str)
        and (pair[1] is None or isinstance(pair[1], str))
        for pair in pairs
    ):
        raise AcceptanceUnreadable
    return [pair[0] for pair in pairs]


def log_dropped(what: str, timing: ReadTiming) -> None:
    """Log a dropped result by its timing and token counts alone."""
    logger.info(
        "%s was dropped after %.1f s, %d tokens in, %d out",
        what,
        timing.seconds,
        timing.input_tokens,
        timing.output_tokens,
    )
