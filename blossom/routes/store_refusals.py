# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Gerardo Bodegas Martinez
"""How a route tells a busy household file from any other refusal of it, and what a press says
when its read of the household's records is refused.

The import pages, and the grade pages' choice of the term shown, answer a refused write by
whether the file was busy: another program held it, so nothing was written. Any other refusal,
or one with no code, says less, since the outcome may not be known.
"""

import sqlite3
from typing import Final

RECORDS_UNREAD: Final = "Blossom couldn't read the household's records."
"""Said for a press whose read of the household's records was refused, busy or not: nothing
about a save, or about whether the text was read, or about what is shown below it."""
BUSY_CODES: Final = frozenset({sqlite3.SQLITE_BUSY})
"""The primary result codes of a household file another program holds."""


def busy_file(error: sqlite3.Error) -> bool:
    """Whether the household's file refused as busy, by its error's primary code, as the drafts
    store reads it. An error with no code, as one made in Python, is not busy."""
    code = getattr(error, "sqlite_errorcode", None)
    return isinstance(code, int) and (code & 0xFF) in BUSY_CODES
