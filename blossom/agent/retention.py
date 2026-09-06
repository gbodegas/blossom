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

The sweep at startup applies both rules to whatever a crash left behind. A
draft saved but never published belongs to a run that died on the way: if its
thread paused at the gate, which the interrupt left pending on the saved
state shows, the sweep publishes it as the run would have, several in the
order their checkpoints say they paused, and if the thread is missing, never
reached the draft, or holds the draft without having paused, the sweep takes
it back, since nothing could ever review it. A waiting draft whose thread holds
a review that never landed in the table, because recording it failed, has that
review recorded, since a review that reached the thread is never lost and
never expired away. Then any thread no waiting draft refers to is cleared,
which covers runs that ended without their thread being removed and runs that
never finished.

The same sweep runs on a schedule while the process is up, when runs may be in
flight. A run between saving its draft and pausing with it looks, from the
tables, like a run that died there, so the caller names the threads it is
running and the sweep leaves them and their drafts alone.
"""

from collections.abc import Collection
from dataclasses import dataclass
from datetime import date, timedelta
from typing import Any, Final

from langgraph.checkpoint.base import BaseCheckpointSaver

from blossom.clock import Clock
from blossom.drafts import Decision, DraftStatus
from blossom.stores.drafts import DraftRecord, DraftsStore

PAUSED_RETENTION_DAYS: Final = 14
"""How long a draft may wait at the gate past its evening before it is closed as expired."""

EXPIRED_REASON: Final = "the evening passed without a decision"


@dataclass(frozen=True)
class Swept:
    """What one sweep did: drafts closed as expired, threads cleared, drafts taken back."""

    expired: tuple[str, ...]
    cleared: tuple[str, ...]
    withdrawn: tuple[str, ...] = ()
    """Drafts left waiting by a run that died before pausing with them, now taken back."""
    finished: tuple[str, ...] = ()
    """Drafts whose thread held a review that had not landed in the table, now recorded."""
    published: tuple[str, ...] = ()
    """Drafts whose run paused and died before publishing them, now published."""


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


def reviewable(saved: SavedThread | None) -> bool:
    """Whether a review could resume the thread: it paused at the gate with the draft."""
    return saved is not None and saved.paused and saved.has_draft


async def finish_held_reviews(
    checkpointer: BaseCheckpointSaver[Any],
    drafts: DraftsStore,
    *,
    plan_date: date | None = None,
    in_flight: Collection[str] = (),
) -> list[tuple[str, str]]:
    """Record every review a waiting draft's thread holds that the table never got.

    Returns the draft and thread of each review recorded, so the caller can
    clear the threads: a review recorded is a decision landed, and the thread
    has nothing left to do. Over one evening when ``plan_date`` is given, so a
    run about to publish a plan can finish the reviews its plan would otherwise
    take the place of, and over every evening for the sweep. Runs in flight are
    left alone.
    """
    finished: list[tuple[str, str]] = []
    for record in drafts.waiting():
        if record.thread_id in in_flight:
            continue
        if plan_date is not None and record.plan_date != plan_date:
            continue
        saved = await saved_thread(checkpointer, record.thread_id)
        if saved is None or saved.held is None:
            continue
        decision, reason = saved.held
        approved = decision == "approved"
        drafts.record_decision(
            record.draft_id,
            status=DraftStatus.APPROVED_FOR_MANUAL_SEND if approved else DraftStatus.DRAFT,
            decision=decision,
            reason=reason,
        )
        finished.append((record.draft_id, record.thread_id))
    return finished


async def sweep_saved_state(
    checkpointer: BaseCheckpointSaver[Any],
    drafts: DraftsStore,
    clock: Clock,
    *,
    in_flight: Collection[str] = (),
) -> Swept:
    """Finish or take back what a run left, close what waited too long, clear the rest.

    ``in_flight`` names the threads of runs the caller is running right now;
    they and their drafts are left alone, since a run between saving its draft
    and pausing with it is not a run that died there. Empty at startup.

    Reviews a thread holds that the table never recorded are finished first,
    so nothing published afterwards can take the place of a draft a parent had
    in fact reviewed. Then an unpublished draft whose run is not in flight,
    which belongs to a run that died on the way, is published if its thread
    paused at the gate, the interrupt still pending, several in the order
    their checkpoints were written, which is the order they paused in; and
    taken back if its thread is missing, short of the draft, or holds the draft
    without the pause, since no review could resume it. A published waiting
    draft no review could resume, which only a file from before publication
    can hold, is taken back too.
    """
    finished = await finish_held_reviews(checkpointer, drafts, in_flight=in_flight)

    published: list[str] = []
    withdrawn: list[str] = []
    cleared: list[str] = []
    paused: list[tuple[str, DraftRecord]] = []
    for record in drafts.unpublished():
        if record.thread_id in in_flight:
            continue
        saved = await saved_thread(checkpointer, record.thread_id)
        if reviewable(saved):
            paused.append((saved.saved_at if saved else "", record))
        else:
            drafts.withdraw(record.draft_id)
            withdrawn.append(record.draft_id)
    for _, record in sorted(
        paused, key=lambda item: (item[0], item[1].created_at, item[1].draft_id)
    ):
        for displaced in drafts.publish(record.draft_id):
            await checkpointer.adelete_thread(displaced.thread_id)
            cleared.append(displaced.thread_id)
        published.append(record.draft_id)

    for record in drafts.waiting():
        if record.thread_id in in_flight:
            continue
        if not reviewable(await saved_thread(checkpointer, record.thread_id)):
            drafts.withdraw(record.draft_id)
            withdrawn.append(record.draft_id)

    today = clock.today()
    expired: list[str] = []
    for record in drafts.waiting():
        if record.plan_date + timedelta(days=PAUSED_RETENTION_DAYS) < today:
            drafts.record_decision(
                record.draft_id, status=DraftStatus.DRAFT, decision="expired", reason=EXPIRED_REASON
            )
            expired.append(record.draft_id)

    keep = {record.thread_id for record in drafts.waiting()} | set(in_flight)
    saved_threads: set[str] = set()
    async for item in checkpointer.alist(None):
        saved_threads.add(str(item.config["configurable"]["thread_id"]))
    for thread_id in sorted(thread_id for thread_id in saved_threads if thread_id not in keep):
        await checkpointer.adelete_thread(thread_id)
        cleared.append(thread_id)
    return Swept(
        expired=tuple(expired),
        cleared=tuple(sorted(cleared)),
        withdrawn=tuple(withdrawn),
        finished=tuple(draft_id for draft_id, _ in finished),
        published=tuple(published),
    )
