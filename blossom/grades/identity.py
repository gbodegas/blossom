# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Gerardo Bodegas Martinez
"""Whether a grade report's student line is one a parent confirmed as hers, told without her name.

A line is compared as its name form: a keyed hash of the line with its spaces, case and accents
folded, under a key drawn from the household secret with a label no passphrase can be, so it is
none of the keys the sign-in draws. The key check, a keyed hash of a fixed text, says whether the
confirmed forms were made under the key in hand. When it doesn't match, the secret was replaced,
and her name is confirmed again; nothing here guesses why, and a mismatch never reads as another
student.
"""

import hmac
import unicodedata
from collections.abc import Collection
from dataclasses import dataclass
from enum import StrEnum
from typing import Final

from blossom.grades.draft import folded

NAME_FORM_LABEL: Final = b"\xffgrade name forms"
"""What the name-form key is drawn for, under the household secret. Its first byte is never
valid UTF-8, and a passphrase always is, so no sign-in key is drawn from the same input."""
KEY_CHECK_TEXT: Final = b"\xffthe key the confirmed name forms were made under"
"""The fixed text the key check is a keyed hash of. Its first byte is never valid UTF-8, and a
student line always is, so no line's form is ever the key check."""


class IdentityStatus(StrEnum):
    """What a student line is, against her record."""

    MATCHES = "matches"
    """A form a parent confirmed, under a key whose check matches."""
    FIRST_USE = "first_use"
    """No form is confirmed yet: the question is "Is this her?"."""
    NOT_CONFIRMED = "not_confirmed"
    """Forms are confirmed, and none of them is this line's."""
    CONFIRM_AGAIN = "confirm_again"
    """The key check doesn't match the key: her name is to be confirmed again."""
    MISSING = "missing"
    """The report has no student line."""


@dataclass(frozen=True)
class Identity:
    """What a student line is, with the keyed form of the line asked about, so an answer can be
    held to the line it answered. A missing line has no form."""

    status: IdentityStatus
    form: str | None


def name_form_key(secret: bytes) -> bytes:
    """The key name forms are made under: a keyed hash of its own label under ``secret``."""
    return hmac.new(secret, NAME_FORM_LABEL, "sha256").digest()


def name_form(key: bytes, student_line: str) -> str:
    """The line's name form, in hex: a keyed hash of the line with each run of spaces read as
    one space, its case folded and its accents written one way however they were typed, so
    the name itself is never what is kept."""
    decomposed = unicodedata.normalize("NFD", folded(student_line))
    text = unicodedata.normalize("NFD", decomposed.casefold())
    return hmac.new(key, text.encode("utf-8"), "sha256").hexdigest()


def key_check(key: bytes) -> str:
    """A keyed hash of a fixed text under ``key``, in hex: it tells keys apart and keeps
    neither."""
    return hmac.new(key, KEY_CHECK_TEXT, "sha256").hexdigest()


def _same(held: str, made: str) -> bool:
    """Whether a hash on record is the one made now, compared in constant time."""
    return hmac.compare_digest(held.encode("utf-8"), made.encode("utf-8"))


def identity_among(
    key: bytes, student_line: str | None, *, check: str | None, forms: Collection[str]
) -> Identity:
    """What ``student_line`` is, given her key check on record (``None`` before the first form)
    and her confirmed forms. A line that folds to nothing is no line. Forms on record that no
    key check vouches for are to be confirmed again."""
    if student_line is None or not folded(student_line):
        return Identity(IdentityStatus.MISSING, None)
    form = name_form(key, student_line)
    if check is None and not forms:
        return Identity(IdentityStatus.FIRST_USE, form)
    if check is None or not _same(check, key_check(key)):
        return Identity(IdentityStatus.CONFIRM_AGAIN, form)
    if any(_same(held, form) for held in forms):
        return Identity(IdentityStatus.MATCHES, form)
    return Identity(IdentityStatus.NOT_CONFIRMED if forms else IdentityStatus.FIRST_USE, form)
