"""Where a paused graph's saved state, and any draft it holds, lives on disk.

A graph that pauses at the approval gate has to survive the process that paused
it. Its state goes into a SQLite file of its own, separate from project state,
so the two writers never share pages and clearing one thread's history touches
nothing else.

A word on names. The framework calls each saved snapshot a checkpoint and its
classes carry that word, so the code here does too. The prose says saved graph
state, because the parent's view at ``/parent/checkpoint`` is a different thing
and the two should not read alike.

Three properties are decided here rather than left to defaults.

The saver is the asynchronous one, built inside the application lifespan,
because it binds to the running event loop at construction and the web app
drives graphs asynchronously. The synchronous saver's async methods raise.

Deserialization is strict. Saved state records every value with the module and
class that produced it and reconstructs the class by import on load, so a
database that anyone else can write is a way to run code. The serializer here
accepts only the types the graph is known to carry; anything else comes back as
plain data rather than an object. The framework reads its own strict-mode flag
from the environment at import time, which is too early and too easy to leave
unset, so the allowlist is passed to the constructor.

Deleted rows are overwritten (``secure_delete``), and the file may not live on
a network share or inside a folder a sync client owns: a write-ahead log on
either is a documented way to corrupt a database, and a synced copy would carry
a student's schoolwork off the machine.

Retention is not built: nothing here clears an old thread. The saver's only
pruning primitive deletes a thread whole, and a retention rule will call that.
"""

import os
from collections.abc import AsyncIterator, Mapping
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Final

import aiosqlite
from langgraph.checkpoint.serde.jsonplus import JsonPlusSerializer
from langgraph.checkpoint.sqlite.aio import AsyncSqliteSaver

from blossom.drafts import Draft, DraftStatus

BUSY_TIMEOUT_SECONDS: Final = 5.0
"""How long a write waits on a lock before failing, instead of the driver's default."""

STATE_TYPES: Final[tuple[type, ...]] = (Draft, DraftStatus)
"""Every class a graph may carry in its saved state. Adding one is a reviewed edit."""

# Names a sync client gives its folders, matched case-insensitively as a
# substring of each part of the path, since a work account's folder is called
# "OneDrive - <organization>". The environment variables are the roots the
# client itself reports.
SYNCED_FOLDER_MARKERS: Final[frozenset[str]] = frozenset(
    {"onedrive", "dropbox", "google drive", "googledrive", "icloud"}
)
SYNC_ROOT_VARIABLES: Final[tuple[str, ...]] = ("OneDrive", "OneDriveConsumer", "OneDriveCommercial")


class UnsafeCheckpointPath(ValueError):
    """Raised at startup for a path that cannot hold the saved-state database safely."""


def refuse_unsafe_path(path: Path, environ: Mapping[str, str] | None = None) -> Path:
    """Return ``path`` if it may hold the saved-state database; raise otherwise.

    Refused: an in-memory database (nothing survives the process, which defeats
    the point of saving state), a network share, and any location inside a
    folder a sync client owns.
    """
    text = str(path)
    if text == ":memory:":
        msg = "saved graph state must live in a file; an in-memory database survives nothing"
        raise UnsafeCheckpointPath(msg)
    if text.startswith(("\\\\", "//")):
        msg = f"saved graph state may not live on a network share: {text}"
        raise UnsafeCheckpointPath(msg)
    if any(marker in part.lower() for part in path.parts for marker in SYNCED_FOLDER_MARKERS):
        msg = f"saved graph state may not live inside a synced folder: {text}"
        raise UnsafeCheckpointPath(msg)
    env = os.environ if environ is None else environ
    normalized = os.path.normcase(text)
    for variable in SYNC_ROOT_VARIABLES:
        root = env.get(variable, "").strip()
        if root and normalized.startswith(os.path.normcase(root).rstrip(os.sep) + os.sep):
            msg = f"saved graph state may not live under {variable}: {text}"
            raise UnsafeCheckpointPath(msg)
    return path


def checkpoint_serializer() -> JsonPlusSerializer:
    """The serializer all saved state goes through: strict, with the graph's own types."""
    return JsonPlusSerializer(allowed_msgpack_modules=STATE_TYPES)


@asynccontextmanager
async def open_checkpointer(path: Path) -> AsyncIterator[AsyncSqliteSaver]:
    """Open the saved-state store for the life of the application.

    Must be entered inside a running event loop; the saver captures the loop it
    is constructed on. The connection is closed on exit so shutdown does not
    hang on it.
    """
    safe = refuse_unsafe_path(path)
    safe.parent.mkdir(parents=True, exist_ok=True)
    connection = await aiosqlite.connect(safe, timeout=BUSY_TIMEOUT_SECONDS)
    try:
        await connection.execute("PRAGMA secure_delete=ON")
        yield AsyncSqliteSaver(connection, serde=checkpoint_serializer())
    finally:
        await connection.close()
