# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Gerardo Bodegas Martinez
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
only as far as a small bound in bytes and in time: it never reads a body the
first way, whole and unbounded, and nothing it finds or fails to find changes
the refusal.

A body the form parser can't read at all, multipart it rejects, a part it can't decode in
the charset the body names, too many fields, or a field too long, is not a form, and no page
can say what it held. Nor is one whose charset decodes a part into half of a character, text
no page can show and no record keeps. Reading one raises
``FormUnreadable``, wherever the form is read: in a handler, through ``form_of``, or by the
framework for a route whose fields are parameters, through ``FormRoute``. The application
answers it with one page, which reads no store. The parser's own log lines can quote the
body it refuses, so they are kept out of the log.
"""

import asyncio
import logging
from collections.abc import AsyncGenerator, Callable, Coroutine
from contextlib import aclosing
from typing import Any, Final

from fastapi import Depends, Request, Response
from fastapi.routing import APIRoute
from python_multipart.exceptions import FormParserError
from starlette.datastructures import FormData, Headers
from starlette.exceptions import HTTPException
from starlette.formparsers import FormParser, MultiPartException
from starlette.requests import ClientDisconnect

TOKEN_MAX_LENGTH: Final = 200
"""Longer than any id the store makes or a seed carries; a longer one is not looked up."""
COPY_LIMIT: Final = 16 * 1024
"""The most of a refused press's body its copy reads, in bytes as sent."""
COPY_DEADLINE: Final = 5.0
"""How long, in seconds, a refused press's copy may take to be read. The time is counted
once, from the moment its body starts being read, and a chunk that arrives does not start
it again: past it the copy is given up and the refusal stands without one."""
URL_ENCODED: Final = "application/x-www-form-urlencoded"
"""The one kind of body a refused press's copy is read from: the kind a page's form sends."""
PARSER_LOGGER: Final = "python_multipart"
"""The logger the form parser writes through, with each of its modules' loggers beneath it."""


def quiet_the_parser() -> None:
    """Keep the form parser's own lines out of the log. Its warnings name a byte of the body
    it refuses, as a number or a character, and the application logs each refusal by its
    kind alone. Only the parser's logger changes."""
    logging.getLogger(PARSER_LOGGER).setLevel(logging.CRITICAL + 1)


class FormUnreadable(Exception):
    """A body the form parser could not read, named by the kind of the parser's failure.
    Nothing of the body is kept, and the parser's own words are never said, since they can
    quote it."""


def parser_refused(error: HTTPException) -> bool:
    """Whether a 400 came from the form parser refusing the body: the parser's own failure,
    a part it can't decode, or the one its reader turns into a 400. Anything else, a client
    gone among them, is not a form that could not be read."""
    return error.status_code == 400 and isinstance(
        error.__context__, FormParserError | UnicodeError | MultiPartException
    )


def written(form: FormData) -> FormData:
    """The form, once every text the parser decoded in it, a field's name, its value, or a
    file's name, is text UTF-8 can write; ``UnicodeEncodeError`` when one holds half of a
    character. A file's bytes are never decoded, so they are not text here. Nothing is
    changed."""
    for name, value in form.multi_items():
        name.encode("utf-8")
        if isinstance(value, str):
            value.encode("utf-8")
        elif value.filename is not None:
            value.filename.encode("utf-8")
    return form


async def form_of(request: Request) -> FormData:
    """The request's form as the parser reads it, or ``FormUnreadable`` when the parser can't,
    or when the text it read can't be written."""
    try:
        return written(await request.form())
    except (FormParserError, UnicodeError) as error:
        raise FormUnreadable(type(error).__name__) from error
    except HTTPException as error:
        if parser_refused(error):
            raise FormUnreadable(type(error.__context__).__name__) from error
        raise


async def form_read(request: Request) -> None:
    """A route's first dependency: the form the framework has read, through ``form_of``, so
    text that can't be written is ``FormUnreadable`` before the handler runs."""
    await form_of(request)


class FormRoute(APIRoute):
    """A route whose form fields are parameters, so the framework reads the form before the
    handler runs: a body the parser can't read is ``FormUnreadable`` here too, and so is text
    in it that can't be written, before any other dependency of the route."""

    def __init__(
        self,
        path: str,
        endpoint: Callable[..., Any],
        **options: Any,  # noqa: ANN401  (the framework's own options, passed on as given)
    ) -> None:
        given = list(options.pop("dependencies", None) or ())
        if not any(depends.dependency is form_read for depends in given):
            given.insert(0, Depends(form_read))
        super().__init__(path, endpoint, dependencies=given, **options)

    def get_route_handler(self) -> Callable[[Request], Coroutine[Any, Any, Response]]:
        """The framework's handler, with the parser's refusal of the body said as one."""
        handle = super().get_route_handler()

        async def read(request: Request) -> Response:
            try:
                return await handle(request)
            except HTTPException as error:
                if parser_refused(error):
                    raise FormUnreadable(type(error.__context__).__name__) from error
                raise

        return read


async def fields_of(
    request: Request,
    allowed: frozenset[str],
    *,
    may_be_absent: frozenset[str] = frozenset(),
    ignored: Callable[[str], bool] | None = None,
) -> tuple[dict[str, str], bool]:
    """The form's fields, and whether the form was whole.

    Whole means every field is one this form sends, comes once, is text,
    and that every field the form sends came: a field sent twice, a field
    of another form's, a channel among them, an uploaded file, or a field
    left out makes it not whole, and nothing is written for such a form.
    ``may_be_absent`` names the fields a browser leaves out of the page's
    own form, a group of radio buttons with none chosen; the caller asks
    for the choice. A name ``ignored`` matches is passed over unread, as if
    it weren't sent. The first text value of each allowed field is still
    handed back, so the page that refuses can keep what was typed. A body the parser can't
    read is ``FormUnreadable``.
    """
    form = await form_of(request)
    fields: dict[str, str] = {}
    whole = True
    for name, value in form.multi_items():
        if ignored is not None and ignored(name):
            continue
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
    ever held, whatever a client sends. Reading and parsing share one deadline,
    ``COPY_DEADLINE``, started as the body starts being read: a body that has not all come
    by then is given up, what came of it is dropped, and nothing goes on reading. Only that
    deadline is caught here; a request canceled from outside stays canceled. Each allowed
    field keeps its first value, decoded as every form here is, bytes that are not UTF-8
    read as the replacement character. Nothing is checked beyond that, nothing is looked
    up, and nothing is logged.
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
    try:
        async with asyncio.timeout(COPY_DEADLINE):
            body = await bounded_body(request, declared)
            if body is None:
                return None
            return await fields_in(body, headers, allowed)
    except TimeoutError:
        return None


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
