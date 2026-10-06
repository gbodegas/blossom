# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Gerardo Bodegas Martinez
"""Where a state file may live, and where it may not.

Every file the household's state lives in goes through one guard at startup:
not in memory, not on a network share whether named as one or mapped to a
drive letter, and not inside a folder a sync client owns. A write-ahead log on
a share or in a synced folder is a documented way to corrupt a database, and a
synced copy would carry the family's record somewhere it was never meant to
go. The saved-state store, the record of assignments, the drafts, the traces,
her signals, her requests for help, the household's claim on its files, and
the sign-in secret all open through it.

A ``TEST-COPY`` file declares that its folder holds a test copy: files copied from the
household's, never linked to them. Before a start opens anything, with the test-copy flag
on, the folder each state file really lands in must hold one, and no existing state file
may be a link or anything but a plain file; with the flag off, a marker in a configured or
real folder is refused. The check can't tell where the data came from. It holds for copies
on local storage that no other process changes while Blossom checks or uses them. A marker
put in the household's own folder, files swapped by another process, and network mounts
are outside it.
"""

import ctypes
import os
import stat
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

    A share and a mapped drive letter are asked about before the path is
    resolved, since resolving it reaches the share. The rest runs against the
    resolved path, because a junction or a symlink can point a plainly named
    folder at a synced one, and the sync client follows where the folder really
    is rather than how it was spelled.
    """
    text = str(path)
    if text == ":memory:":
        msg = "state must live in a file; an in-memory database survives nothing"
        raise UnsafeCheckpointPath(msg)
    # A share is read from the configured text before anything resolves it: it
    # is named the same way everywhere, while resolving a Windows path on
    # another platform turns it into an ordinary local name. The drive letter a
    # path lands on, relative or not, is asked about without resolving it too.
    absolute = without_extended_prefix(os.path.abspath(text))
    if looks_like_a_share(text) or drive_is_network(absolute):
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


SECRET_NAME: Final = "household.secret"  # noqa: S105  (a file name, not a secret)
"""The sign-in secret's file, beside the household's database."""
SQLITE_SIDECARS: Final = ("-journal", "-wal", "-shm")
"""What SQLite opens beside a database by name: its journal, its write-ahead log, and the
log's index. Each database is checked for all three, whatever journal mode it runs in."""
TEST_COPY_MARKER: Final = "TEST-COPY"
"""The empty file that declares a folder holds a test copy, put there by whoever makes the
copy. It belongs in no folder of the household's own."""


class CopyMarkError(ValueError):
    """Raised at startup when the test-copy flag and the state folders disagree."""


def lock_path_for(state_path: Path) -> Path:
    """The lock file beside a state file, named for the file as it really is.

    ``blossom.sqlite3`` is claimed through ``blossom.lock`` in the same
    folder. The path is resolved first, so a file reached by two spellings,
    through a link or a relative path, is claimed through one lock.
    """
    return state_path.resolve().with_suffix(".lock")


def state_files(database: Path, checkpoint: Path, trace: Path) -> list[Path]:
    """Every file a start opens in a state folder: the three databases with their SQLite
    files, the two locks the claim holds, and the sign-in secret."""
    files = [
        path.with_name(path.name + suffix)
        for path in (database, checkpoint, trace)
        for suffix in ("", *SQLITE_SIDECARS)
    ]
    files += [lock_path_for(database), lock_path_for(checkpoint), database.with_name(SECRET_NAME)]
    return list(dict.fromkeys(files))


def holds_a_marker(folder: Path) -> bool:
    """Whether ``folder`` holds ``TEST-COPY`` in any case, for a file system that tells case
    apart. A folder that isn't there holds nothing."""
    try:
        names = os.listdir(folder)
    except (FileNotFoundError, NotADirectoryError):
        return False
    return any(name.casefold() == TEST_COPY_MARKER.casefold() for name in names)


def not_plain(path: Path) -> str | None:
    """What makes an existing state file unfit for a test copy, or ``None`` when it is absent
    or a plain file with one name. A link count that can't be read counts against it."""
    try:
        status = os.lstat(path)
    except FileNotFoundError:
        return None
    if stat.S_ISLNK(status.st_mode):
        return "a symbolic link"
    if not stat.S_ISREG(status.st_mode):
        return "not a plain file"
    if status.st_nlink != 1:
        return f"a file whose link count is {status.st_nlink}, not 1"
    return None


def landing(path: Path) -> tuple[Path, str]:
    """The folder ``path`` really lands in, and how a refusal names it: by the configured
    folder, or by the file and its real folder when the two differ."""
    real = Path(os.path.realpath(path)).parent
    if os.path.normcase(os.path.abspath(path.parent)) == os.path.normcase(real):
        return real, str(path.parent)
    return real, f"{path} lands in {real}, which"


def refuse_mismarked_state(
    database: Path, checkpoint: Path, trace: Path, *, test_copy: bool
) -> None:
    """Refuse a test copy its marks and files don't bear out, and a marked copy unlabeled.

    The paths pass ``refuse_unsafe_path`` first. After that only names and file details are
    read, where each state file really lands: nothing is opened, created or claimed.
    """
    for path in (database, checkpoint, trace):
        refuse_unsafe_path(path)
    files = state_files(database, checkpoint, trace)
    if test_copy:
        for path in files:
            real, named = landing(path)
            if not (real / TEST_COPY_MARKER).is_file():
                msg = (
                    f"BLOSSOM_TEST_COPY is on, but {named} holds no {TEST_COPY_MARKER} file: "
                    "a test copy runs only on state copied into a folder marked as one"
                )
                raise CopyMarkError(msg)
        for path in files:
            problem = not_plain(path)
            if problem is not None:
                msg = (
                    f"BLOSSOM_TEST_COPY is on, but {path} is {problem}: a test copy's state "
                    "files are its own copies, never links"
                )
                raise CopyMarkError(msg)
        return
    checked: set[str] = set()
    for path in files:
        real, named = landing(path)
        for folder, said in ((path.parent, str(path.parent)), (real, named)):
            key = os.path.normcase(os.path.abspath(folder))
            if key in checked:
                continue
            checked.add(key)
            if holds_a_marker(folder):
                msg = (
                    f"{said} holds a {TEST_COPY_MARKER} file, so its state is a test copy: "
                    "set BLOSSOM_TEST_COPY=1 to serve it"
                )
                raise CopyMarkError(msg)
