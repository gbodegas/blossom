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
adds the graph's own types to the framework's built-in safe set, and a class
outside both comes back as plain data rather than an object. The framework
reads its own strict-mode flag
from the environment at import time, which is too early and too easy to leave
unset, so the allowlist is passed to the constructor.

Deleted rows are overwritten (``secure_delete``), and the file may not live on
a network share or inside a folder a sync client owns: a write-ahead log on
either is a documented way to corrupt a database, and a synced copy would carry
a student's schoolwork off the machine.

Retention is not built: nothing here clears an old thread. The saver's only
pruning primitive deletes a thread whole, and a retention rule will call that.
"""

import ctypes
import os
from collections.abc import AsyncIterator, Mapping
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Final

import aiosqlite
from langgraph.checkpoint.serde.jsonplus import JsonPlusSerializer
from langgraph.checkpoint.sqlite.aio import AsyncSqliteSaver

from blossom.drafts import Draft, DraftStatus
from blossom.heuristic_relevance import Criterion, CriterionFinding, CriticVerdict, Judgment
from blossom.plan_checks import PlanCheck, PlanVerification
from blossom.plans import DailyPlan, Deferral, PlanBlock
from blossom.reconciliation import SourceConfidence
from blossom.stores.project_state import Assignment, AssignmentKind
from blossom.verification import CheckOutcome

BUSY_TIMEOUT_SECONDS: Final = 5.0
"""How long a write waits on a lock before failing, instead of the driver's default."""

STATE_TYPES: Final[tuple[type, ...]] = (
    Assignment,
    AssignmentKind,
    SourceConfidence,
    DailyPlan,
    PlanBlock,
    Deferral,
    PlanVerification,
    PlanCheck,
    CheckOutcome,
    CriticVerdict,
    CriterionFinding,
    Criterion,
    Judgment,
    Draft,
    DraftStatus,
)
"""Every class a graph may carry in its saved state. Adding one is a reviewed edit."""

# Folder names a sync client uses, matched case-insensitively against a whole
# path component, or against one that starts with the name and a separator,
# since a work account's folder is called "OneDrive - <organization>" and the
# macOS mount is "OneDrive-Personal". Matching a bare substring instead would
# read an ordinary folder called "MyOneDriveBackups" as a synced one.
SYNCED_FOLDER_MARKERS: Final[frozenset[str]] = frozenset(
    {
        "onedrive",
        "dropbox",
        "google drive",
        "googledrive",
        "icloud drive",
        "iclouddrive",
        "com~apple~clouddocs",
        "mobile documents",
    }
)
SYNC_ROOT_VARIABLES: Final[tuple[str, ...]] = ("OneDrive", "OneDriveConsumer", "OneDriveCommercial")

DRIVE_REMOTE: Final = 4
"""What Windows calls a drive letter mapped to a network share."""


class UnsafeCheckpointPath(ValueError):
    """Raised at startup for a path that cannot hold the saved-state database safely."""


def drive_is_network(text: str) -> bool:
    """True when a Windows path sits on a drive letter mapped to a share.

    A mapped drive is a network share wearing a local name, so the letter has
    to be asked about rather than read. Returns False anywhere but Windows.
    """
    if os.name != "nt":
        return False
    drive = os.path.splitdrive(text)[0]
    if not drive.endswith(":"):
        return False
    windll = getattr(ctypes, "windll", None)
    if windll is None:
        return False
    return int(windll.kernel32.GetDriveTypeW(drive + os.sep)) == DRIVE_REMOTE


def is_synced_folder(part: str) -> bool:
    """True when one path component is a folder a sync client owns."""
    lowered = part.lower()
    return any(
        lowered == marker or lowered.startswith((f"{marker} -", f"{marker}-"))
        for marker in SYNCED_FOLDER_MARKERS
    )


def without_extended_prefix(text: str) -> str:
    """The same path with the Windows extended-length prefix removed.

    ``\\\\?\\C:\\...`` is a local path spelled the long way, and only
    ``\\\\?\\UNC\\...`` is a share, so the prefix comes off before anything
    reads the shape of what is left.
    """
    if text.startswith("\\\\?\\"):
        text = text[4:]
        if text.startswith("UNC\\"):
            text = "\\\\" + text[4:]
    return text


def looks_like_a_share(text: str) -> bool:
    """True for a path named as a network share, in either slash."""
    return without_extended_prefix(text).startswith(("\\\\", "//"))


def local_form(path: Path) -> str:
    """Where the path really lands, with any extended-length prefix removed."""
    return without_extended_prefix(os.path.realpath(path))


def refuse_unsafe_path(path: Path, environ: Mapping[str, str] | None = None) -> Path:
    """Return ``path`` if it may hold the saved-state database; raise otherwise.

    Refused: an in-memory database (nothing survives the process, which defeats
    the point of saving state), a network share whether named as one or mapped
    to a drive letter, and any location inside a folder a sync client owns.

    Every test but the first runs against the resolved path, because a junction
    or a symlink can point a plainly named folder at a synced one, and the sync
    client follows where the folder really is rather than how it was spelled.
    """
    text = str(path)
    if text == ":memory:":
        msg = "saved graph state must live in a file; an in-memory database survives nothing"
        raise UnsafeCheckpointPath(msg)
    # A share is read from the configured text before anything resolves it: it
    # is named the same way everywhere, while resolving a Windows path on
    # another platform turns it into an ordinary local name.
    if looks_like_a_share(text):
        msg = f"saved graph state may not live on a network share: {text}"
        raise UnsafeCheckpointPath(msg)
    real = local_form(path)
    if looks_like_a_share(real) or drive_is_network(real):
        msg = f"saved graph state may not live on a network share: {text}"
        raise UnsafeCheckpointPath(msg)
    if any(is_synced_folder(part) for part in Path(real).parts):
        msg = f"saved graph state may not live inside a synced folder: {text}"
        raise UnsafeCheckpointPath(msg)
    env = os.environ if environ is None else environ
    normalized = os.path.normcase(real)
    for variable in SYNC_ROOT_VARIABLES:
        root = env.get(variable, "").strip()
        if root and normalized.startswith(os.path.normcase(os.path.realpath(root)) + os.sep):
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
