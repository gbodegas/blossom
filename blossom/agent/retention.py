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
draft left waiting by a run that died after saving it and before pausing with
it, whose thread is missing or never reached the draft, is taken back first,
since nothing could ever review it; taking one back can leave another waiting
in its stead, so the pass repeats until every waiting draft has a thread that
could review it. A waiting draft whose thread holds a review that never landed
in the table, because recording it failed, has that review recorded, since a
review that reached the thread is never lost and never expired away. Then any
thread no waiting draft refers to is cleared, which covers runs that ended
without their thread being removed and runs that never finished.

The same sweep runs on a schedule while the process is up, when runs may be in
flight. A run between saving its draft and pausing with it looks, from the
tables, like a run that died there, so the caller names the threads it is
running and the sweep leaves them, their drafts, and the threads of the drafts
they have displaced alone: a run that pauses clears those itself, and a run
that fails gives the displaced draft back, thread and all.
"""

from collections.abc import Collection
from dataclasses import dataclass
from datetime import timedelta
from typing import Any, Final

from langgraph.checkpoint.base import BaseCheckpointSaver

from blossom.agent.runs import draft_id_for
from blossom.clock import Clock
from blossom.drafts import Decision, DraftStatus
from blossom.stores.drafts import DraftsStore

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


async def clear_thread(checkpointer: BaseCheckpointSaver[Any], thread_id: str) -> None:
    """Remove one thread's saved state, once nothing will resume it."""
    await checkpointer.adelete_thread(thread_id)


async def saved_values(
    checkpointer: BaseCheckpointSaver[Any], thread_id: str
) -> dict[str, Any] | None:
    """The channel values of a thread's latest checkpoint, or ``None`` for a missing thread."""
    saved = await checkpointer.aget_tuple({"configurable": {"thread_id": thread_id}})
    if saved is None:
        return None
    return dict(saved.checkpoint["channel_values"])


async def paused_with_its_draft(checkpointer: BaseCheckpointSaver[Any], thread_id: str) -> bool:
    """Whether a thread's saved state carries the draft, so a review could resume it.

    A run saves its draft in the drafts file and then writes the checkpoint
    that carries it; a run that died between the two left a waiting draft with
    a thread that is missing or stops before the draft. Such a thread cannot
    be resumed into a review, and the draft was never anyone's plan.
    """
    values = await saved_values(checkpointer, thread_id)
    return values is not None and values.get("draft") is not None


async def held_review(
    checkpointer: BaseCheckpointSaver[Any], thread_id: str
) -> tuple[Decision, str | None] | None:
    """The review a thread holds past the gate when its record never landed, or ``None``.

    The gate writes the decision and its reason into saved state before the
    node after it records them in the table; a failure between the two leaves
    the thread holding a review the table does not know about.
    """
    values = await saved_values(checkpointer, thread_id)
    if values is None:
        return None
    decision = values.get("decision")
    if decision not in ("approved", "rejected"):
        return None
    reason = values.get("reason")
    return decision, reason if isinstance(reason, str) else None


async def sweep_saved_state(
    checkpointer: BaseCheckpointSaver[Any],
    drafts: DraftsStore,
    clock: Clock,
    *,
    in_flight: Collection[str] = (),
) -> Swept:
    """Take back drafts no thread can review, close those that waited too long, clear the rest.

    ``in_flight`` names the threads of runs the caller is running right now;
    they and their drafts are left alone, since a run between saving its draft
    and pausing with it is not a run that died there. Empty at startup.
    """
    withdrawn: list[str] = []
    while True:
        orphaned = [
            record
            for record in drafts.waiting()
            if record.thread_id not in in_flight
            and not await paused_with_its_draft(checkpointer, record.thread_id)
        ]
        if not orphaned:
            break
        for record in orphaned:
            drafts.withdraw(record.draft_id)
            withdrawn.append(record.draft_id)

    finished: list[str] = []
    for record in drafts.waiting():
        if record.thread_id in in_flight:
            continue
        held = await held_review(checkpointer, record.thread_id)
        if held is None:
            continue
        decision, reason = held
        approved = decision == "approved"
        drafts.record_decision(
            record.draft_id,
            status=DraftStatus.APPROVED_FOR_MANUAL_SEND if approved else DraftStatus.DRAFT,
            decision=decision,
            reason=reason,
        )
        finished.append(record.draft_id)

    today = clock.today()
    expired: list[str] = []
    for record in drafts.waiting():
        if record.plan_date + timedelta(days=PAUSED_RETENTION_DAYS) < today:
            drafts.record_decision(
                record.draft_id, status=DraftStatus.DRAFT, decision="expired", reason=EXPIRED_REASON
            )
            expired.append(record.draft_id)

    keep = {record.thread_id for record in drafts.waiting()} | set(in_flight)
    for thread_id in in_flight:
        keep.update(record.thread_id for record in drafts.displaced_by(draft_id_for(thread_id)))
    saved: set[str] = set()
    async for item in checkpointer.alist(None):
        saved.add(str(item.config["configurable"]["thread_id"]))
    cleared = sorted(thread_id for thread_id in saved if thread_id not in keep)
    for thread_id in cleared:
        await checkpointer.adelete_thread(thread_id)
    return Swept(
        expired=tuple(expired),
        cleared=tuple(cleared),
        withdrawn=tuple(withdrawn),
        finished=tuple(finished),
    )
