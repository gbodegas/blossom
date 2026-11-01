# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Gerardo Bodegas Martinez
"""How a route tells a busy household file from any other refusal of it, what a press says
when its read of the household's records is refused, and which errors are the household's store
failing at all.

The import pages, and the grade pages' choice of the term shown, answer a refused write by
whether the file was busy: another program held it, so nothing was written. Any other refusal,
or one with no code, says less, since the outcome may not be known. The decide press tells a
refused read before its write the same way: a busy one says the record couldn't be read in
time, and any other says the records sentence.

A failure no page's own recovery catches is answered by the application's fallback only when
it is the store failing, not a mistake in the code that called it.
"""

import sqlite3
from typing import Final

from blossom.stores.drafts import StoreBusy, WriterBusy

RECORDS_UNREAD: Final = "Blossom couldn't read the household's records."
"""Said for a press whose read of the household's records was refused, busy or not, or for the
decide press when it wasn't busy: nothing about a save, or about whether the text was read, or
about what is shown below it."""
BUSY_CODES: Final = frozenset({sqlite3.SQLITE_BUSY})
"""The primary result codes of a household file another program holds."""
STORE_FAILURE_CODES: Final = frozenset(
    {
        sqlite3.SQLITE_BUSY,
        sqlite3.SQLITE_PERM,
        sqlite3.SQLITE_READONLY,
        sqlite3.SQLITE_IOERR,
        sqlite3.SQLITE_CORRUPT,
        sqlite3.SQLITE_FULL,
        sqlite3.SQLITE_CANTOPEN,
        sqlite3.SQLITE_PROTOCOL,
        sqlite3.SQLITE_NOTADB,
    }
)
"""The primary result codes of a household file that can't be used: held by another program,
refused by its permissions or as read-only, failing to read or write, damaged, on a full disk,
not opened, or not a database."""


def busy_file(error: sqlite3.Error) -> bool:
    """Whether the household's file refused as busy, by its error's primary code, as the drafts
    store reads it. An error with no code, as one made in Python, is not busy."""
    code = getattr(error, "sqlite_errorcode", None)
    return isinstance(code, int) and (code & 0xFF) in BUSY_CODES


def store_failure(error: BaseException) -> bool:
    """Whether ``error`` is the household's store failing: the drafts store's lock or the file's
    writer staying taken for the whole wait; any sqlite3 error whose primary code, an extended
    code read by it, is one of ``STORE_FAILURE_CODES``, whatever its class; or an
    ``OperationalError`` with no code, as the drafts store makes for a commit past its wait and
    a connection makes for stored text that isn't UTF-8. Any other code, a mistake in SQL or a
    statement left open among them, any other error with no code, and anything that isn't a
    sqlite3 error, is not."""
    if isinstance(error, StoreBusy | WriterBusy):
        return True
    if not isinstance(error, sqlite3.Error):
        return False
    code = getattr(error, "sqlite_errorcode", None)
    if isinstance(code, int):
        return (code & 0xFF) in STORE_FAILURE_CODES
    return isinstance(error, sqlite3.OperationalError)
