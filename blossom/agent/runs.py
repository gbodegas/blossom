# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Gerardo Bodegas Martinez
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

A run also has a time limit, ``RUN_DEADLINE_SECONDS``, held by a ``RunBudget``
that travels in the graph's context: every planner and reviewer request, and
every retry, gets only what is left of it, and the run is timed node by node.
"""

import asyncio
import time
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass, field
from typing import Final

from langchain_core.callbacks import BaseCallbackHandler
from langchain_core.runnables import RunnableConfig
from langgraph.types import Durability, StateSnapshot

from blossom.agent.steps import RunTiming, StageTime
from blossom.anthropic_client import ServiceBusy

GRAPH_VERSION: Final = 1
"""Bumped whenever a change would mislead a thread paused under the old graph."""

GRAPH_VERSION_KEY: Final = "graph_version"

THREAD_ID_KEY: Final = "thread_id"
"""The thread id's name in a run's metadata, where the tracer reads it."""

RECURSION_LIMIT: Final = 15
"""Supersteps allowed per run: room for a handful of nodes and two critic rounds."""

DURABILITY: Final[Durability] = "sync"
"""Save the state before the next step starts.

Passed to ``ainvoke`` beside the configuration; the framework defaults to
``async``, which lets a crash lose the step that recorded a decision."""


RUN_DEADLINE_SECONDS: Final = 90.0
"""The most one run may take, every planning attempt, review, and retry included. A
ceiling, not a target: an ordinary run should finish well inside it."""

MODEL_RETRIES: Final = 2
"""How many times one request is sent again when the service is busy, inside the limit."""

RETRY_PAUSE_SECONDS: Final = 0.5
"""The wait before the first retry; each later one waits twice as long, never past the limit."""


class RunTimedOut(TimeoutError):
    """The run's time ran out while it waited for a model, or before it could ask one."""

    def __init__(self) -> None:
        super().__init__(f"the run's {RUN_DEADLINE_SECONDS:g} seconds ran out")


@dataclass
class RunBudget:
    """One run's time, read from a monotonic clock, and what the run spent of it.

    Made when the run starts, so every request and every retry draws on the same
    limit and nothing starts the clock again. ``clock`` and ``sleep`` are the
    process's own unless a test gives others.
    """

    seconds: float = RUN_DEADLINE_SECONDS
    clock: Callable[[], float] = time.monotonic
    sleep: Callable[[float], Awaitable[None]] = asyncio.sleep
    started: float = field(init=False)
    stages: list[StageTime] = field(default_factory=list)
    model_calls: int = 0
    retries: int = 0
    outputs: list[int] = field(default_factory=list)
    waiting_on: tuple[str, int] | None = None
    """The node and round of the request in progress, kept when one ends the run."""
    _lap: float = field(init=False)

    def __post_init__(self) -> None:
        self.started = self._lap = self.clock()

    def elapsed(self) -> float:
        """Seconds since the run started."""
        return self.clock() - self.started

    def remaining(self) -> float:
        """Seconds left before the limit, never below nothing."""
        return max(0.0, self.seconds - self.elapsed())

    def lap(self, node: str, round_number: int) -> None:
        """Note that ``node`` ended now, timed from the end of the node before it."""
        now = self.clock()
        seconds = round(now - self._lap, 3)
        self.stages.append(StageTime(node=node, round=round_number, seconds=seconds))
        self._lap = now

    async def ask[T](self, call: Callable[[], Awaitable[T]], *, stage: str, round_number: int) -> T:
        """Make one request within what is left, asking again while the service is busy.

        No request starts once the limit is reached, and none runs past it: one
        still waiting then is canceled, one that held the event loop past it has
        its answer set aside, busy or not, and the run has timed out. A busy service
        is asked again at most ``MODEL_RETRIES`` times, after a pause cut to what
        is left; the last busy answer is raised when retries are spent. A
        ``TimeoutError`` the request raises itself is raised as it is.
        """
        self.waiting_on = (stage, round_number)
        attempt = 0
        while True:
            left = self.remaining()
            if left <= 0:
                raise RunTimedOut
            self.model_calls += 1
            if attempt:
                self.retries += 1
            limit = asyncio.timeout(left)
            try:
                async with limit:
                    answer = await call()
            except TimeoutError as error:
                if limit.expired():
                    raise RunTimedOut from error
                raise
            except ServiceBusy:
                # A busy answer that came back after the limit times the run out instead.
                if attempt == MODEL_RETRIES and self.elapsed() <= self.seconds:
                    raise
            else:
                # The timeout can only cancel a request at an await, so an answer that
                # came back after the limit is checked here.
                if self.elapsed() > self.seconds:
                    raise RunTimedOut
                self.waiting_on = None
                return answer
            await self.sleep(min(RETRY_PAUSE_SECONDS * 2**attempt, self.remaining()))
            attempt += 1

    def timing(self, category: str | None) -> RunTiming:
        """The run's record of time and requests, as it stands now."""
        return RunTiming(
            seconds=round(self.elapsed(), 3),
            stages=self.stages,
            model_calls=self.model_calls,
            retries=self.retries,
            output_tokens=sum(self.outputs) if self.outputs else None,
            largest_output_tokens=max(self.outputs) if self.outputs else None,
            category=category,
        )


class StaleGraphVersion(RuntimeError):
    """Raised when a thread was written by a different graph version than the one running."""


def draft_id_for(thread_id: str) -> str:
    """The one draft a thread can produce, named after it.

    A node run twice names the same row, and a route that needs to take a
    draft back after its run failed can name it without the graph.
    """
    return f"draft:{thread_id}"


def run_config(
    thread_id: str,
    *,
    recursion_limit: int = RECURSION_LIMIT,
    callbacks: Sequence[BaseCallbackHandler] = (),
) -> RunnableConfig:
    """The configuration every run is invoked with.

    The graph version and the thread id are what reach the saved metadata, in
    plaintext; the thread id is also saved in a column of its own, and the
    recursion limit is not saved at all. Nothing about the student belongs in
    any of them. The thread id is stated in the metadata rather than left for
    the framework to copy there, because the tracer reads it from the run's
    metadata and the copy is the framework's habit, not its promise. Run-scoped
    objects travel through the graph's context, which is not saved;
    ``callbacks`` is where the local tracer rides, and it is never saved either.
    """
    config: RunnableConfig = {
        "configurable": {THREAD_ID_KEY: thread_id},
        "metadata": {GRAPH_VERSION_KEY: GRAPH_VERSION, THREAD_ID_KEY: thread_id},
        "recursion_limit": recursion_limit,
    }
    if callbacks:
        config["callbacks"] = list(callbacks)
    return config


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
