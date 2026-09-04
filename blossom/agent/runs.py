"""The contract every graph run carries: a thread, a version, and hard limits.

Saved graph state outlives the code that wrote it. A thread paused at the gate
tonight is resumed by whatever version of the graph is running tomorrow, and
the framework makes no promise about that: it versions its own storage format
and nothing else. A renamed node makes resume a silent no-op; a node inserted
before the gate is skipped for every paused thread; a renamed state class comes
back as a dictionary. So Blossom versions its graphs itself.

``GRAPH_VERSION`` is written into the metadata a run saves, and
``ensure_current_version`` refuses to resume a thread written by another
version. The rules that let the version stay put are in
``docs/architecture.md``: state grows only by optional or reducer keys, values
gain fields only with defaults and are never renamed or moved, and the node
names ahead of a gate are part of the contract. Breaking any of them means
bumping the version and draining paused threads rather than resuming them.

The recursion limit is set here because the framework's default is not a
safeguard: it is ten thousand and seven supersteps, read from the environment
at import like the strict flag the saved-state store contends with, and a
routing mistake in a planner-critic loop would make that many model calls
before failing. ``DURABILITY`` is ``sync`` so the state is on disk before the next
step starts, rather than while it runs; on one machine with a small graph the
throughput cost is nothing and the guarantee that a recorded decision survives
a crash is the point. It is passed at the call site, not carried in the
configuration, because the framework takes it as a separate argument and
defaults to ``async`` when it is left out. A scan in
``tests/test_architecture_constraints.py`` refuses a run that builds a
configuration here and then omits it.
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
"""Save the state before the next step starts.

Passed to ``ainvoke`` beside the configuration; the framework defaults to
``async``, which lets a crash lose the step that recorded a decision."""


class StaleGraphVersion(RuntimeError):
    """Raised when a thread was written by a different graph version than the one running."""


def run_config(thread_id: str, *, recursion_limit: int = RECURSION_LIMIT) -> RunnableConfig:
    """The configuration every run is invoked with.

    The graph version is what reaches the saved metadata, in plaintext. The
    thread id is saved in plaintext too, in a column of its own, and the
    recursion limit is not saved at all. Nothing about the student belongs in
    any of them. Run-scoped objects travel through the graph's context, which
    is not saved.
    """
    return {
        "configurable": {"thread_id": thread_id},
        "metadata": {GRAPH_VERSION_KEY: GRAPH_VERSION},
        "recursion_limit": recursion_limit,
    }


def recorded_version(snapshot: StateSnapshot) -> int | None:
    """The graph version a thread's latest saved state was written under, if any.

    Metadata is stored as plain JSON, outside the strict serializer, so anything
    at all may be in the field. Only a whole number counts, and ``bool`` is
    excluded although Python calls it one, so a hand-written ``true`` cannot
    read as a version.
    """
    metadata = snapshot.metadata or {}
    value = metadata.get(GRAPH_VERSION_KEY)
    if isinstance(value, bool) or not isinstance(value, int):
        return None
    return value


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
