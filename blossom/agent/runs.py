"""The contract every graph run carries: a thread, a version, and hard limits.

Checkpoints outlive the code that wrote them. A thread paused at the gate
tonight is resumed by whatever version of the graph is running tomorrow, and
the framework makes no promise about that: it versions its own checkpoint
envelope and nothing else. A renamed node makes resume a silent no-op; a node
inserted before the gate is skipped for every paused thread; a renamed state
class comes back as a dictionary. So Blossom versions its graphs itself.

``GRAPH_VERSION`` is written into the metadata of every checkpoint a run
produces, and ``ensure_current_version`` refuses to resume a thread written by
another version. The rules that let the version stay put are in
``docs/architecture.md``: state grows only by optional or reducer keys, values
gain fields only with defaults and are never renamed or moved, and the node
names ahead of a gate are part of the contract. Breaking any of them means
bumping the version and draining paused threads rather than resuming them.

The recursion limit is set here because the framework's default is not a
safeguard: the installed default is ten thousand supersteps, and a routing
mistake in a planner-critic loop would make that many model calls before
failing. Durability is ``sync`` so a checkpoint is on disk before the next
step starts; on one machine with a small graph the throughput cost is nothing
and the guarantee that a recorded decision survives a crash is the point.
"""

from typing import Final

from langchain_core.runnables import RunnableConfig
from langgraph.types import Durability, StateSnapshot

GRAPH_VERSION: Final = 1
"""Bumped whenever a change would mislead a thread paused under the old graph."""

GRAPH_VERSION_KEY: Final = "graph_version"

RECURSION_LIMIT: Final = 15
"""Supersteps allowed per run: room for a handful of nodes and two critic rounds."""

DURABILITY: Final[Durability] = "sync"
"""Persist each checkpoint before the next step starts."""


class StaleGraphVersion(RuntimeError):
    """Raised when a thread was written by a different graph version than the one running."""


def run_config(thread_id: str, *, recursion_limit: int = RECURSION_LIMIT) -> RunnableConfig:
    """The configuration every run is invoked with.

    Only scalar values placed here are persisted into checkpoint metadata, in
    plaintext, so nothing about the student belongs in it: a thread id, the
    graph version, and the limit. Run-scoped objects travel through the graph's
    context, which is not persisted.
    """
    return {
        "configurable": {"thread_id": thread_id},
        "metadata": {GRAPH_VERSION_KEY: GRAPH_VERSION},
        "recursion_limit": recursion_limit,
    }


def recorded_version(snapshot: StateSnapshot) -> int | None:
    """The graph version a thread's latest checkpoint was written under, if any."""
    metadata = snapshot.metadata or {}
    value = metadata.get(GRAPH_VERSION_KEY)
    return value if isinstance(value, int) else None


def ensure_current_version(snapshot: StateSnapshot) -> None:
    """Refuse to continue a thread written by another version of the graph.

    A silent resume under a changed graph is the failure this guards against;
    the caller decides what to do with the thread, which is usually to re-queue
    what it held and delete it.
    """
    recorded = recorded_version(snapshot)
    if recorded != GRAPH_VERSION:
        msg = (
            f"this thread was written by graph version {recorded!r}; the running graph is "
            f"version {GRAPH_VERSION}. Resuming it could skip or misread a step."
        )
        raise StaleGraphVersion(msg)
