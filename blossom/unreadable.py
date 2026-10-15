# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Gerardo Bodegas Martinez
"""What a store says about a kept row it can't read, in names alone.

A row that fails to decode may hold her words in any column, and both a pydantic
refusal and sqlite3's own refusal of a text column that isn't UTF-8 repeat the
value they refused. A store names the model that refused the row, each field it
refused, and the kind of refusal, and nothing the row held. It raises its own
error after the except block, so the refusal is neither its cause nor its context.
"""

import sqlite3
from collections.abc import Collection

from pydantic import ValidationError


def refusal_in_names(fault: Exception, fields: Collection[str]) -> str:
    """The kind of failure, or the model that refused a row with each of ``fields`` it
    refused and the kind of refusal. Any other key in a refusal's place came from the row
    and is left out."""
    if not isinstance(fault, ValidationError):
        return type(fault).__name__
    refusals = []
    for error in fault.errors(include_url=False, include_context=False, include_input=False):
        place = ".".join(
            str(part) for part in error["loc"] if isinstance(part, int) or part in fields
        )
        refusals.append(f"{place} {error['type']}" if place else error["type"])
    return f"{fault.title}: {', '.join(refusals)}"


class TextNotUtf8(sqlite3.OperationalError):
    """A text column refused because its bytes aren't UTF-8. It is an ``OperationalError``, as
    sqlite3 refuses such text, so whatever handles one handles this; a store that decodes a
    row can tell it apart from a failure of the file itself."""


def text_or_refusal(raw: bytes) -> str:
    """A text column as its UTF-8, for a connection's ``text_factory``. Bytes that aren't
    UTF-8 are refused as sqlite3 refuses them, an ``OperationalError`` at the read, in words
    that repeat none of the column."""
    return _utf8_or(raw, sqlite3.OperationalError)


def text_or_named_refusal(raw: bytes) -> str:
    """``text_or_refusal`` for a store that passes over a row it can't decode: the refusal is
    raised as ``TextNotUtf8``, which sqlite3 raises from the read as it is."""
    return _utf8_or(raw, TextNotUtf8)


def _utf8_or(raw: bytes, refusal: type[sqlite3.OperationalError]) -> str:
    try:
        return raw.decode("utf-8")
    except UnicodeDecodeError:
        msg = "a text column holds bytes that are not UTF-8"
    raise refusal(msg)
