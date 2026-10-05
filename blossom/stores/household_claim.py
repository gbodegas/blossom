# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Gerardo Bodegas Martinez
"""One process serves a household, and the process says so by holding its files.

The drafts file and the saved-state file are shared state, and what keeps them
consistent lives in one process: the decision lock and the sweep are held in
its memory, the work it detaches, such as a plan graph still unwinding or a
run's ending, goes on in its own threads and event loop, and each run's
deadline is an instant on its monotonic clock. A second process over the same
files would share none of it: its start would end a run the first is still
running as interrupted, its sweep could then clear that run's thread while the
first still writes to it, and it would weigh the first's deadlines against its
own clock. So a process claims the household at startup by taking an exclusive
lock on a file beside the drafts file, holds it for as long as it runs, and a
second process is refused with a sentence naming the file, before it opens
anything.

The lock is the operating system's, so a process that dies releases it, and
nothing stale has to be cleaned up by hand. Both files that make up the
household's state are claimed, the drafts file and the saved-state file, since
each can be pointed elsewhere on its own, and the path is resolved before the
lock file is named, so two spellings of one file claim one lock.
"""

import sys
from pathlib import Path
from typing import IO

from blossom.stores.paths import refuse_unsafe_path

if sys.platform == "win32":
    import msvcrt

    def _lock(handle: IO[bytes]) -> None:
        handle.seek(0)
        msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)

    def _unlock(handle: IO[bytes]) -> None:
        handle.seek(0)
        msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)

else:
    import fcntl

    def _lock(handle: IO[bytes]) -> None:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)

    def _unlock(handle: IO[bytes]) -> None:
        fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


class AnotherProcessHasTheHousehold(RuntimeError):
    """Raised at startup when another process holds the household's files."""

    def __init__(self, lock_path: Path) -> None:
        super().__init__(
            f"another Blossom process has this household's files open; {lock_path} is held. "
            "One process serves a household: stop the other, or point this one at other files."
        )
        self.lock_path = lock_path


class HouseholdClaim:
    """An exclusive claim on a household's files, held until released."""

    def __init__(self, *lock_paths: Path) -> None:
        self.lock_paths = lock_paths
        self._handles: list[IO[bytes]] = []
        for lock_path in lock_paths:
            lock_path.parent.mkdir(parents=True, exist_ok=True)
            handle: IO[bytes] = lock_path.open("a+b")
            try:
                _lock(handle)
            except OSError as error:
                handle.close()
                self.release()
                raise AnotherProcessHasTheHousehold(lock_path) from error
            self._handles.append(handle)

    def release(self) -> None:
        """Let another process claim the household; called when this one stops."""
        handles, self._handles = self._handles, []
        for handle in handles:
            try:
                _unlock(handle)
            finally:
                handle.close()


def lock_path_for(state_path: Path) -> Path:
    """The lock file beside a state file, named for the file as it really is.

    ``blossom.sqlite3`` is claimed through ``blossom.lock`` in the same
    folder. The path is resolved first, so a file reached by two spellings,
    through a link or a relative path, is claimed through one lock.
    """
    return state_path.resolve().with_suffix(".lock")


def claim_household(*state_paths: Path) -> HouseholdClaim:
    """Claim the household whose state lives in ``state_paths``, or refuse.

    Each path passes the guard the stores apply first, so a place the state
    may not live, an in-memory database, a network share, a synced folder, is
    refused before any lock file is opened there. One lock per distinct file,
    in the order given, so a claim refused on the second releases the first
    before it raises.
    """
    safe = [refuse_unsafe_path(path) for path in state_paths]
    return HouseholdClaim(*dict.fromkeys(lock_path_for(path) for path in safe))
