# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Gerardo Bodegas Martinez
"""What saved graph state keeps, and for how long.

A graph's saved state is the loop's short-term memory: the evening, the plan,
the checks, the pause at the gate. It is kept while the loop runs and cleared
when the loop finishes, because whatever should outlive the run has already
been written elsewhere: the draft and the decision in the drafts table, the
run's record beside them, the framework's trace in its own file. So a thread
is cleared as soon as its run stops before the gate, and a thread that paused
at the gate is cleared once a decision is recorded.

A thread waiting at the gate waits as long as its draft does, but not past the
point where the evening is long gone. ``PAUSED_RETENTION_DAYS`` after its plan
date, an undecided draft is closed as expired and its thread cleared. Nobody
decided, and the record says so rather than pretending someone did.

The sweep at startup applies both rules to whatever a crash left behind,
after the runs a stopped process left running are ended. It never publishes
and never deletes a draft: only a run's own settlement puts a plan on the
pages, and a published plan stays on her page even when its thread is gone. A
waiting draft whose thread holds a review that never landed in the table,
because recording it failed, has that review recorded, since a review that
reached the thread is never lost and never expired away. Runs past their
deadline are ended. Then any thread that neither a waiting draft nor a run
still running refers to is cleared, which covers runs that ended without their
thread being removed.

The same sweep runs on a schedule while the process is up, when runs may be
running. The threads are listed before the runs still running are read, so a
run admitted in between is either missed by the listing or kept by the read.
Every call on the drafts store runs on a worker thread.
"""

import asyncio
import logging
import time
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import date, timedelta
from typing import Any, Final

from langgraph.checkpoint.base import BaseCheckpointSaver

from blossom.agent.runs import GRAPH_VERSION, GRAPH_VERSION_KEY, to_the_end
from blossom.clock import Clock
from blossom.drafts import Decision, DraftStatus
from blossom.stores.drafts import DraftsStore

logger = logging.getLogger(__name__)

PAUSED_RETENTION_DAYS: Final = 14
"""How long a draft may wait at the gate past its evening before it is closed as expired."""

EXPIRED_REASON: Final = "the evening passed without a decision"


@dataclass(frozen=True)
class Swept:
    """What one sweep did: drafts closed as expired, threads cleared, reviews finished."""

    expired: tuple[str, ...]
    cleared: tuple[str, ...]
    finished: tuple[str, ...] = ()
    """Drafts whose thread held a review that had not landed in the table, now recorded."""
    ended: tuple[str, ...] = ()
    """Runs still running past their deadline, now ended."""


async def clear_thread(checkpointer: BaseCheckpointSaver[Any], thread_id: str) -> None:
    """Remove one thread's saved state, once nothing will resume it."""
    await checkpointer.adelete_thread(thread_id)


INTERRUPT_CHANNEL: Final = "__interrupt__"
"""The framework's name for the pending write an interrupt leaves on a thread's
latest checkpoint. A thread paused at the gate carries one; a thread whose
process died after the checkpoint before the gate and before the gate paused
does not, though its saved state holds the draft."""


@dataclass(frozen=True)
class SavedThread:
    """What a thread's latest saved state says about its run, as much as the sweep needs."""

    has_draft: bool
    """The state carries the draft: the run got past ``compose``."""
    paused: bool
    """An interrupt is pending: the gate paused, and a review could resume it."""
    held: tuple[Decision, str | None] | None
    """A review the gate passed on that the table never recorded, with its reason."""
    saved_at: str
    """When the latest checkpoint was written, as the framework stamps it. The
    checkpoint before the gate is written in the instant before the gate
    pauses, so this is the order runs paused in, as far as anything durable
    records it."""


async def saved_thread(
    checkpointer: BaseCheckpointSaver[Any], thread_id: str
) -> SavedThread | None:
    """Read a thread's latest checkpoint, or ``None`` for a thread the saver does not hold."""
    saved = await checkpointer.aget_tuple({"configurable": {"thread_id": thread_id}})
    if saved is None:
        return None
    values = saved.checkpoint["channel_values"]
    decision = values.get("decision")
    held: tuple[Decision, str | None] | None = None
    if decision in ("approved", "rejected"):
        reason = values.get("reason")
        held = (decision, reason if isinstance(reason, str) else None)
    return SavedThread(
        has_draft=values.get("draft") is not None,
        paused=any(channel == INTERRUPT_CHANNEL for _, channel, _ in saved.pending_writes or ()),
        held=held,
        saved_at=str(saved.checkpoint["ts"]),
    )


def unresumable_threads(
    checkpointer: BaseCheckpointSaver[Any], thread_ids: Sequence[str], wait: float
) -> frozenset[str]:
    """The threads no review could resume, for a page read on a worker thread: those the
    saver positively doesn't hold, or holds as written by another version of the graph.

    The reads share ``wait``. A thread whose read fails or doesn't finish in time is left
    out, as is every thread when this runs on an event loop's own thread, so a page keeps a
    decision's buttons and the decision's own refusal stays the authority.
    """
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        pass
    else:
        return frozenset()
    loop: asyncio.AbstractEventLoop | None = getattr(checkpointer, "loop", None)
    ends = time.monotonic() + wait
    found: set[str] = set()
    for thread_id in thread_ids:
        try:
            if loop is None:
                saved = checkpointer.get_tuple({"configurable": {"thread_id": thread_id}})
            else:
                reading = asyncio.run_coroutine_threadsafe(
                    checkpointer.aget_tuple({"configurable": {"thread_id": thread_id}}), loop
                )
                try:
                    saved = reading.result(timeout=max(0.0, ends - time.monotonic()))
                except BaseException:
                    reading.cancel()
                    raise
        except Exception as error:
            logger.warning("thread %s could not be read for the page: %s", thread_id, error)
            continue
        version = None if saved is None else (saved.metadata or {}).get(GRAPH_VERSION_KEY)
        if saved is None or isinstance(version, bool) or version != GRAPH_VERSION:
            found.add(thread_id)
    return frozenset(found)


def reviewable(saved: SavedThread | None) -> bool:
    """Whether a review could resume the thread: it paused at the gate with the draft."""
    return saved is not None and saved.paused and saved.has_draft


async def finish_held_reviews(
    checkpointer: BaseCheckpointSaver[Any],
    drafts: DraftsStore,
    *,
    plan_date: date | None = None,
    to_the_end_of_each: bool = True,
) -> list[tuple[str, str]]:
    """Record every review a waiting draft's thread holds that the table never got.

    Returns the draft and thread of each review recorded, so the caller can
    clear the threads: a review recorded is a decision landed, and the thread
    has nothing left to do. Over one evening when ``plan_date`` is given, so a
    run about to publish a plan can finish the reviews its plan would otherwise
    take the place of, and over every evening for the sweep. The sweep sees each
    write to its end; a run whose time runs out stops waiting for one, which
    lands or not as the table decides, and the next pass records it if not.
    """
    finished: list[tuple[str, str]] = []
    for record in await asyncio.to_thread(drafts.waiting):
        if plan_date is not None and record.plan_date != plan_date:
            continue
        saved = await saved_thread(checkpointer, record.thread_id)
        if saved is None or saved.held is None:
            continue
        decision, reason = saved.held
        approved = decision == "approved"
        writing = asyncio.to_thread(
            drafts.record_decision,
            record.draft_id,
            status=DraftStatus.APPROVED_FOR_MANUAL_SEND if approved else DraftStatus.DRAFT,
            decision=decision,
            reason=reason,
        )
        await (to_the_end(writing) if to_the_end_of_each else writing)
        finished.append((record.draft_id, record.thread_id))
    return finished


async def sweep_saved_state(
    checkpointer: BaseCheckpointSaver[Any], drafts: DraftsStore, clock: Clock
) -> Swept:
    """Finish held reviews, end runs out of time, close what waited too long, clear the rest.

    The saved threads are listed first. Reviews a thread holds that the table
    never recorded are then finished, so no plan published afterwards takes the
    place of a draft a parent had in fact reviewed, and runs still running past
    their deadline are ended. A waiting draft past its retention closes as
    expired. Last, the waiting drafts and the runs still running are read, and
    every listed thread neither refers to is cleared. Nothing is published, and
    no draft is deleted.
    """
    listed: set[str] = set()
    async for item in checkpointer.alist(None):
        listed.add(str(item.config["configurable"]["thread_id"]))

    finished = await finish_held_reviews(checkpointer, drafts)
    ended = await asyncio.to_thread(drafts.reconcile_runs)

    today = clock.today()
    expired: list[str] = []
    # Subtracting one date from another gives a span and cannot overflow;
    # adding the span to a plan date near the calendar's last day would.
    for record in await asyncio.to_thread(drafts.waiting):
        if today - record.plan_date > timedelta(days=PAUSED_RETENTION_DAYS):
            await asyncio.to_thread(
                drafts.record_decision,
                record.draft_id,
                status=DraftStatus.DRAFT,
                decision="expired",
                reason=EXPIRED_REASON,
            )
            expired.append(record.draft_id)

    waiting = await asyncio.to_thread(drafts.waiting)
    keep = {record.thread_id for record in waiting} | await asyncio.to_thread(
        drafts.running_threads
    )
    cleared: list[str] = []
    for thread_id in sorted(thread_id for thread_id in listed if thread_id not in keep):
        await checkpointer.adelete_thread(thread_id)
        cleared.append(thread_id)
    return Swept(
        expired=tuple(expired),
        cleared=tuple(cleared),
        finished=tuple(draft_id for draft_id, _ in finished),
        ended=tuple(ended),
    )
