"""Where a state file may live, and where it may not.

Every file the household's state lives in goes through one guard at startup:
not in memory, not on a network share whether named as one or mapped to a
drive letter, and not inside a folder a sync client owns. A write-ahead log on
a share or in a synced folder is a documented way to corrupt a database, and a
synced copy would carry the family's record somewhere it was never meant to
go. The saved-state store, the record of assignments, the drafts, the traces,
her signals, her requests for help, the household's claim on its files, and
the sign-in secret all open through it.
"""

import ctypes
import os
from collections.abc import Mapping
from pathlib import Path
from typing import Final

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
    """Raised at startup for a path that cannot hold a state file safely."""


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
    """Return ``path`` if it may hold one of the household's state files; raise otherwise.

    Refused: an in-memory database (nothing survives the process, which defeats
    the point of saving state), a network share whether named as one or mapped
    to a drive letter, and any location inside a folder a sync client owns.

    Every test but the first runs against the resolved path, because a junction
    or a symlink can point a plainly named folder at a synced one, and the sync
    client follows where the folder really is rather than how it was spelled.
    """
    text = str(path)
    if text == ":memory:":
        msg = "state must live in a file; an in-memory database survives nothing"
        raise UnsafeCheckpointPath(msg)
    # A share is read from the configured text before anything resolves it: it
    # is named the same way everywhere, while resolving a Windows path on
    # another platform turns it into an ordinary local name.
    if looks_like_a_share(text):
        msg = f"the household's state may not live on a network share: {text}"
        raise UnsafeCheckpointPath(msg)
    real = local_form(path)
    if looks_like_a_share(real) or drive_is_network(real):
        msg = f"the household's state may not live on a network share: {text}"
        raise UnsafeCheckpointPath(msg)
    if any(is_synced_folder(part) for part in Path(real).parts):
        msg = f"the household's state may not live inside a synced folder: {text}"
        raise UnsafeCheckpointPath(msg)
    env = os.environ if environ is None else environ
    normalized = os.path.normcase(real)
    for variable in SYNC_ROOT_VARIABLES:
        root = env.get(variable, "").strip()
        if root and normalized.startswith(os.path.normcase(os.path.realpath(root)) + os.sep):
            msg = f"the household's state may not live under {variable}: {text}"
            raise UnsafeCheckpointPath(msg)
    return path
