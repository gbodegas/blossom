"""What saved graph state keeps, and for how long.

A graph's saved state is the loop's short-term memory: the evening, the plan,
the checks, the pause at the gate. It is kept while the loop runs and cleared
when the loop finishes, because whatever should outlive the run has already
been written elsewhere: the draft and the decision in the drafts table, the
run's record beside them, the framework's trace in its own file. So a thread
is cleared the moment its run ends, at the gate or before it, and again once a
decision is recorded.

A thread waiting at the gate waits as long as its draft does, but not past the
point where the evening is long gone. ``PAUSED_RETENTION_DAYS`` after its plan
date, an undecided draft is closed as expired and its thread cleared. Nobody
decided, and the record says so rather than pretending someone did.

The sweep at startup applies both rules to whatever a crash left behind. Any
thread no waiting draft refers to is cleared, which covers runs that ended
without their thread being removed and runs that never finished.
"""

from dataclasses import dataclass
from datetime import timedelta
from typing import Any, Final

from langgraph.checkpoint.base import BaseCheckpointSaver

from blossom.clock import Clock
from blossom.drafts import DraftStatus
from blossom.stores.drafts import DraftsStore

PAUSED_RETENTION_DAYS: Final = 14
"""How long a draft may wait at the gate past its evening before it is closed as expired."""

EXPIRED_REASON: Final = "the evening passed without a decision"


@dataclass(frozen=True)
class Swept:
    """What one sweep did: which drafts it closed as expired, which threads it cleared."""

    expired: tuple[str, ...]
    cleared: tuple[str, ...]


async def clear_thread(checkpointer: BaseCheckpointSaver[Any], thread_id: str) -> None:
    """Remove one thread's saved state. Called when its run has ended."""
    await checkpointer.adelete_thread(thread_id)


async def sweep_saved_state(
    checkpointer: BaseCheckpointSaver[Any], drafts: DraftsStore, clock: Clock
) -> Swept:
    """Close drafts that waited too long, then clear every thread no waiting draft needs."""
    today = clock.today()
    expired: list[str] = []
    for record in drafts.waiting():
        if record.plan_date + timedelta(days=PAUSED_RETENTION_DAYS) < today:
            drafts.record_decision(
                record.draft_id, status=DraftStatus.DRAFT, decision="expired", reason=EXPIRED_REASON
            )
            expired.append(record.draft_id)

    keep = {record.thread_id for record in drafts.waiting()}
    saved: set[str] = set()
    async for item in checkpointer.alist(None):
        saved.add(str(item.config["configurable"]["thread_id"]))
    cleared = sorted(thread_id for thread_id in saved if thread_id not in keep)
    for thread_id in cleared:
        await checkpointer.adelete_thread(thread_id)
    return Swept(expired=tuple(expired), cleared=tuple(cleared))
