# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Gerardo Bodegas Martinez
"""How a failure of the household's file is told busy or not, and how it is named in a log, for
the routes and the homework save alike.

Busy is read from the error's primary result code, so an extended busy code counts and a locked
table does not. A failure goes into a log by its kind alone, never its words, which can quote
stored text.
"""

import sqlite3
from typing import Final

BUSY_CODES: Final = frozenset({sqlite3.SQLITE_BUSY})
"""The primary result codes of a household file another program holds."""


def busy_file(error: sqlite3.Error) -> bool:
    """Whether the household's file refused as busy, by its error's primary code, as the drafts
    store reads it. An error with no code, as one made in Python, is not busy."""
    code = getattr(error, "sqlite_errorcode", None)
    return isinstance(code, int) and (code & 0xFF) in BUSY_CODES


def failure_named(error: BaseException) -> str:
    """A failure by its kind alone: its class, and SQLite's name for its code where it has
    one. Never its words, which can quote stored text."""
    name = getattr(error, "sqlite_errorname", None)
    return type(error).__name__ + (f" {name}" if isinstance(name, str) else "")
