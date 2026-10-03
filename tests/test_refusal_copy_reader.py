# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Gerardo Bodegas Martinez
"""The refusal copy's reader on its own, with nothing of the application around it.

``kept_fields_of`` reads a refused press's body within a bound in bytes and a deadline in
time. Through the application a middleware stands between it and the server, cancels a read
left waiting when the answer goes out, and cancels again what was canceled, so these tests
call the reader directly: it gives up at its deadline by itself, leaves no read of its own
waiting, and a read canceled from outside stays canceled.
"""

import asyncio
from collections.abc import Callable, Coroutine
from typing import Any

import pytest
from starlette.requests import Request
from starlette.types import Message, Scope

from blossom.routes import forms

ALLOWED = frozenset({"title"})
BODY = b"title=Wren%27s+worksheet"
GUARD = 3.0
"""How long a test waits for the reader at most: past it, the reader did not give up."""


class Client:
    """A client that sends a body at its own pace and says how many reads it still has
    waiting."""

    def __init__(self, chunks: list[bytes], *, then: str) -> None:
        self.chunks = list(chunks)
        self.then = then
        self.waiting = 0
        self.started = asyncio.Event()

    async def receive(self) -> Message:
        self.waiting += 1
        self.started.set()
        try:
            if self.chunks:
                chunk = self.chunks.pop(0)
                await asyncio.sleep(0)
                last = not self.chunks and self.then == "end"
                return {"type": "http.request", "body": chunk, "more_body": not last}
            await asyncio.Event().wait()
            raise AssertionError  # pragma: no cover
        finally:
            self.waiting -= 1


def request_for(client: Client, length: int) -> Request:
    scope: Scope = {
        "type": "http",
        "method": "POST",
        "path": "/",
        "headers": [
            (b"content-type", forms.URL_ENCODED.encode()),
            (b"content-length", str(length).encode()),
        ],
    }
    return Request(scope, client.receive)


def run(test: Callable[[], Coroutine[Any, Any, None]]) -> None:
    asyncio.run(test())


@pytest.mark.parametrize(
    ("chunks", "then"),
    [([], "stay"), ([BODY[:5]], "stay")],
    ids=["no body arrives", "a part arrives, then nothing"],
)
def test_the_reader_gives_up_by_itself_and_leaves_no_read_waiting(
    chunks: list[bytes], then: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(forms, "COPY_DEADLINE", 0.2)

    async def test() -> None:
        client = Client(chunks, then=then)
        kept = await asyncio.wait_for(
            forms.kept_fields_of(request_for(client, len(BODY)), ALLOWED), GUARD
        )
        await asyncio.sleep(0.05)
        assert kept is None
        assert client.waiting == 0
        assert asyncio.all_tasks() == {asyncio.current_task()}

    run(test)


def test_the_reader_keeps_a_body_whole_within_its_deadline(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(forms, "COPY_DEADLINE", 0.2)

    async def test() -> None:
        client = Client([BODY[:5], BODY[5:]], then="end")
        kept = await asyncio.wait_for(
            forms.kept_fields_of(request_for(client, len(BODY)), ALLOWED), GUARD
        )
        assert kept == {"title": "Wren's worksheet"}
        assert client.waiting == 0

    run(test)


def test_a_read_canceled_from_outside_stays_canceled(monkeypatch: pytest.MonkeyPatch) -> None:
    """A cancel from outside is not the deadline's to catch: the read ends by the cancel it
    was given, which is a ``BaseException`` and no ``Exception``, never with an answer."""
    monkeypatch.setattr(forms, "COPY_DEADLINE", 30.0)

    async def test() -> None:
        client = Client([], then="stay")
        reading = asyncio.ensure_future(
            forms.kept_fields_of(request_for(client, len(BODY)), ALLOWED)
        )
        await asyncio.wait_for(client.started.wait(), GUARD)
        reading.cancel()
        (ended,) = await asyncio.gather(reading, return_exceptions=True)
        assert isinstance(ended, BaseException)
        assert not isinstance(ended, Exception)
        assert client.waiting == 0

    run(test)
