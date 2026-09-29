"""Reading a form whole, for the pages that take a form and nothing else.

A page's form is a fixed set of fields, each sent once, each text, and all
of them sent: a browser sends a hidden field and a text field whether or
not anything is in them. A form that arrives otherwise, a field twice, a
field of another form's, an uploaded file among the fields, or a field
left out, is not the form the page made, and no handler writes for it;
what it did carry is still handed back, so the page that refuses can keep
what was typed. The one field a browser leaves out of a form it made is a
group of radio buttons with none chosen, so a caller names such a field
and says for itself that a choice is needed. Her page and the family page
read their forms through the one function here, so the rule is the same
at every door.

A press by someone the route is not open to is refused before its form is
read, and a refusal about a note can still show what that press typed. That
copy is read by a second function here, only after the refusal is fixed and
only as far as a small bound: it never reads a body the first way, whole
and unbounded, and nothing it finds or fails to find changes the refusal.
"""

from collections.abc import AsyncGenerator
from contextlib import aclosing
from typing import Final

from fastapi import Request
from starlette.datastructures import Headers
from starlette.formparsers import FormParser, MultiPartException
from starlette.requests import ClientDisconnect

TOKEN_MAX_LENGTH: Final = 200
"""Longer than any id the store makes or a seed carries; a longer one is not looked up."""
COPY_LIMIT: Final = 16 * 1024
"""The most of a refused press's body its copy reads, in bytes as sent."""
URL_ENCODED: Final = "application/x-www-form-urlencoded"
"""The one kind of body a refused press's copy is read from: the kind a page's form sends."""


async def fields_of(
    request: Request,
    allowed: frozenset[str],
    *,
    may_be_absent: frozenset[str] = frozenset(),
) -> tuple[dict[str, str], bool]:
    """The form's fields, and whether the form was whole.

    Whole means every field is one this form sends, comes once, is text,
    and that every field the form sends came: a field sent twice, a field
    of another form's, a channel among them, an uploaded file, or a field
    left out makes it not whole, and nothing is written for such a form.
    ``may_be_absent`` names the fields a browser leaves out of the page's
    own form, a group of radio buttons with none chosen; the caller asks
    for the choice. The first text value of each allowed field is still
    handed back, so the page that refuses can keep what was typed.
    """
    form = await request.form()
    fields: dict[str, str] = {}
    whole = True
    for name, value in form.multi_items():
        if name not in allowed or name in fields or not isinstance(value, str):
            whole = False
            continue
        fields[name] = value
    whole = whole and fields.keys() >= allowed - may_be_absent
    return fields, whole


async def kept_fields_of(request: Request, allowed: frozenset[str]) -> dict[str, str] | None:
    """What a press already refused typed, copied from its own request, or ``None``.

    Only a route that has fixed its refusal calls this, and nothing here changes that
    refusal. The body is read only when its headers say it is a page's form of one declared
    length within ``COPY_LIMIT``, with no transfer or content coding and one content type.
    It is then read chunk by chunk and counted as it comes, never past that length: the copy
    is given up the moment the body runs longer than it said, and when it ends shorter, the
    client goes away, or the parser refuses it. So no more than ``COPY_LIMIT`` bytes are
    ever held, whatever a client sends. Each allowed field keeps its first value, decoded
    as every form here is, bytes that are not UTF-8 read as the replacement character.
    Nothing is checked beyond that, nothing is looked up, and nothing is logged.
    """
    headers = request.headers
    kinds = headers.getlist("content-type")
    lengths = headers.getlist("content-length")
    if (
        len(kinds) != 1
        or media_type(kinds[0]) != URL_ENCODED
        or len(lengths) != 1
        or "transfer-encoding" in headers
        or "content-encoding" in headers
    ):
        return None
    declared = declared_length(lengths[0])
    if declared is None:
        return None
    body = await bounded_body(request, declared)
    if body is None:
        return None
    return await fields_in(body, headers, allowed)


def media_type(given: str) -> str:
    """A content type without its parameters, as a form's parser reads it: the part before
    any semicolon, trimmed and in lowercase."""
    return given.split(";", 1)[0].strip().lower()


def declared_length(given: str) -> int | None:
    """A declared length within ``COPY_LIMIT``, as a count of bytes, or ``None`` for one past
    it or not written as plain digits: no sign, no space, no other script's digits."""
    if not (given.isascii() and given.isdigit()):
        return None
    # Judged as text before it is a number, so thousands of digits are refused here rather
    # than handed to a conversion that raises on them.
    significant = given.lstrip("0") or "0"
    if len(significant) > len(str(COPY_LIMIT)) or int(significant) > COPY_LIMIT:
        return None
    return int(significant)


async def bounded_body(request: Request, declared: int) -> bytes | None:
    """The body, read chunk by chunk, when it is exactly ``declared`` bytes long; ``None``
    as soon as a chunk would take it past that, when it ends short of it, or when the
    client goes away. A chunk that would pass the length is never kept."""
    body = bytearray()
    try:
        async with aclosing(request.stream()) as chunks:
            async for chunk in chunks:
                if len(body) + len(chunk) > declared:
                    return None
                body += chunk
    except ClientDisconnect:
        return None
    return bytes(body) if len(body) == declared else None


async def fields_in(
    body: bytes, headers: Headers, allowed: frozenset[str]
) -> dict[str, str] | None:
    """The first value of each allowed field in a form body already read and bounded, by
    the parser every form here goes through; ``None`` when that parser refuses it."""

    async def whole() -> AsyncGenerator[bytes, None]:
        yield body
        yield b""

    try:
        form = await FormParser(headers, whole()).parse()
    except MultiPartException:
        return None
    kept: dict[str, str] = {}
    for name, value in form.multi_items():
        if name in allowed and name not in kept and isinstance(value, str):
            kept[name] = value
    return kept
