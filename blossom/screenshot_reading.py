# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Gerardo Bodegas Martinez
"""A Read press over a screenshot draft: one attempt, one call per image it covers, and each
result stored for the image its call was made for.

The press starts an attempt over the unread images its page named, then reads each in a
worker thread, a few at a time. A call is given at most 90 seconds and no more than the time
left, and a busy service is asked once more while time is left; an answer, valid or not, is
never asked for again. The press returns by the deadline. A call still open then is held until
it ends: the answer's body is checked against the call's time, so the call ends by the deadline
plus up to 90 seconds, subject to the network timeout's guarantees, since connecting, sending
and the answer's head rely on each operation's timeout. The store drops what such a call
brings, since it takes nothing after the deadline: the image reads as not read in time from
the attempt that covered it.
"""

import asyncio
import hashlib
import logging
import time
from collections.abc import Callable, Collection
from typing import Any, Final, NamedTuple

from blossom.anthropic_client import (
    MISSING_KEY,
    ModelUnavailable,
    ScreenshotAnswer,
    model_configured,
    read_screenshot,
)
from blossom.screenshot_prompt import PROMPT_VERSION, SCHEMA_VERSION
from blossom.screenshots import homework_reading
from blossom.settings import Settings
from blossom.stores.drafts import StoreBusy, WriterBusy
from blossom.stores.import_drafts import (
    Attempt,
    AttemptUnderWay,
    DraftExpired,
    DraftNotFound,
    ImportDraftsStore,
    ReadTiming,
    UnknownImage,
    UploadedReading,
)

logger = logging.getLogger(__name__)

PARALLEL_READS: Final = 5
"""The most calls one press has open at once: ten images, the most a review holds, in two
rounds, so a press waits about two calls' time, and the service sees a few at once."""

CALL_TIMEOUT_SECONDS: Final = 90.0
"""The most time one call is given, as the reader was tested, and never more than the time
left before the press's deadline. It is each network operation's timeout and the time the
answer's body is checked against."""

RETRY_PAUSE_SECONDS: Final = 1.0
"""How long a busy service is given before it is asked again, never past the deadline."""

Reader = Callable[[bytes, float], ScreenshotAnswer]
"""One call: a prepared PNG and the seconds it may wait, to an answer or a failure kind."""


class ReadOutcome(NamedTuple):
    """What a press did, as image ids and failure kinds alone, never text from a reading.

    ``unfinished`` images had no result kept by the deadline; ``busy`` ones had a result the
    file's writer couldn't take; ``dropped`` ones had a result the store refused, such as for
    an image removed since. With no attempt started, ``under_way_until`` says when a current
    one ends, or is ``None`` when the press named nothing to read.
    """

    started: bool = False
    under_way_until: float | None = None
    read: tuple[str, ...] = ()
    failed: tuple[tuple[str, str], ...] = ()
    dropped: tuple[str, ...] = ()
    busy: tuple[str, ...] = ()
    unfinished: tuple[str, ...] = ()


async def read_screenshots(
    store: ImportDraftsStore,
    settings: Settings,
    student_id: str,
    draft_id: str,
    image_ids: Collection[str],
    *,
    monotonic: Callable[[], float],
    held: set["asyncio.Future[Any]"],
    reader: Reader | None = None,
    parallel: int = PARALLEL_READS,
) -> ReadOutcome:
    """Read the unread images among ``image_ids`` once, and return by the attempt's deadline.

    With no key nothing starts. A current attempt starts no call. Every call runs as work
    held in ``held``, the application's set of work a request stopped waiting for, until it
    ends. ``monotonic`` is the clock the store's deadline is read on; ``reader`` replaces
    the call for a test.
    """
    if not model_configured(settings):
        raise ModelUnavailable(MISSING_KEY)
    call = reader or (lambda png, timeout: read_screenshot(settings, png, timeout=timeout))
    under_way: float | None = None
    try:
        attempt = await asyncio.to_thread(store.start_attempt, student_id, draft_id, image_ids)
    except AttemptUnderWay as current:
        under_way = current.ends
        attempt = None
    if attempt is None:
        return ReadOutcome(under_way_until=under_way)
    gate = asyncio.Semaphore(max(1, parallel))

    async def one(image_id: str) -> tuple[str, str | None]:
        async with gate:
            return await asyncio.to_thread(
                _read_one, store, student_id, draft_id, attempt, image_id, call, monotonic
            )

    tasks = {asyncio.ensure_future(one(image_id)): image_id for image_id in attempt.covered}
    for task in tasks:
        held.add(task)
        task.add_done_callback(held.discard)
    done, _ = await asyncio.wait(tasks, timeout=max(0.0, attempt.ends - monotonic()))
    found: dict[str, list[Any]] = {"read": [], "failed": [], "dropped": [], "busy": []}
    unfinished: list[str] = []
    for task, image_id in tasks.items():
        status, failure = task.result() if task in done else ("unfinished", None)
        if status == "failed":
            found["failed"].append((image_id, failure))
        elif status in found:
            found[status].append(image_id)
        else:
            unfinished.append(image_id)
    return ReadOutcome(
        started=True,
        read=tuple(found["read"]),
        failed=tuple(found["failed"]),
        dropped=tuple(found["dropped"]),
        busy=tuple(found["busy"]),
        unfinished=tuple(unfinished),
    )


def _read_one(
    store: ImportDraftsStore,
    student_id: str,
    draft_id: str,
    attempt: Attempt,
    image_id: str,
    call: Reader,
    monotonic: Callable[[], float],
) -> tuple[str, str | None]:
    """Read one image in a worker thread and store its result: its status, and the failure
    kind when one was stored. An error nothing expects is logged by its kind alone."""
    unexpected: str | None = None
    try:
        return _read_and_store(store, student_id, draft_id, attempt, image_id, call, monotonic)
    except Exception as error:
        unexpected = type(error).__name__
    logger.error("a screenshot reading ended with %s", unexpected)
    return ("unfinished", None)


def _read_and_store(
    store: ImportDraftsStore,
    student_id: str,
    draft_id: str,
    attempt: Attempt,
    image_id: str,
    call: Reader,
    monotonic: Callable[[], float],
) -> tuple[str, str | None]:
    began = monotonic()
    try:
        png = store.image_png(student_id, draft_id, image_id)
    except (UnknownImage, DraftNotFound, DraftExpired):
        return ("dropped", None)
    except (StoreBusy, WriterBusy):
        return ("busy", None)
    answer: ScreenshotAnswer | None = None
    tokens_in = tokens_out = 0
    for asked in (1, 2):
        left = attempt.ends - monotonic()
        if left <= 0:
            break
        answer = call(png, min(CALL_TIMEOUT_SECONDS, left))
        tokens_in += answer.input_tokens
        tokens_out += answer.output_tokens
        if answer.failure != "service_busy" or asked == 2:
            break
        time.sleep(max(0.0, min(RETRY_PAUSE_SECONDS, attempt.ends - monotonic())))
    if answer is None:
        return ("unfinished", None)
    timing = ReadTiming(monotonic() - began, tokens_in, tokens_out)
    sha256 = hashlib.sha256(png).hexdigest()
    failure: str | None = answer.failure
    try:
        if failure is None:
            checked = homework_reading(answer.answer)
            if checked.reading is not None:
                uploaded = UploadedReading(
                    kind=checked.reading.image_kind,
                    prompt_version=PROMPT_VERSION,
                    schema_version=SCHEMA_VERSION,
                    body=checked.reading.model_dump_json(),
                )
                kept = store.store_reading(
                    student_id,
                    draft_id,
                    attempt_id=attempt.attempt_id,
                    image_id=image_id,
                    sha256=sha256,
                    reading=uploaded,
                    timing=timing,
                )
                return ("read" if kept else "dropped", None)
            failure = checked.failure
        kept = store.store_failure(
            student_id,
            draft_id,
            attempt_id=attempt.attempt_id,
            image_id=image_id,
            sha256=sha256,
            failure=str(failure),
            timing=timing,
        )
    except (StoreBusy, WriterBusy):
        return ("busy", None)
    return ("failed" if kept else "dropped", failure)
