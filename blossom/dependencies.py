"""Application-scoped objects, built once at startup and injected per request.

Construction happens once, in the application lifespan, and routes receive
what they need through ``Depends``. Building stores inside a request handler
would open a connection and re-seed fixtures on every call, and every new
route would copy the pattern. The lifespan also gives tests a seam: overriding
``get_application_state`` swaps the whole backing world without touching the
environment or the filesystem.

Two stores, two disciplines. The project state connection is shared across
FastAPI's worker threads, which run synchronous path operations, so it is
opened with ``check_same_thread=False`` and ``ProjectStateStore`` serializes
access with a lock. The saved-state store is the asynchronous saver from
``blossom/stores/checkpoints.py``: it binds to the event loop it is built on,
so it is opened inside the lifespan and must be used only from asynchronous
handlers. A route that drives a graph is ``async def``; a route that reads
project state need not be.
"""

import asyncio
import logging
import sqlite3
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import AbstractAsyncContextManager, asynccontextmanager
from dataclasses import dataclass, field
from typing import Final, cast

from fastapi import FastAPI, Request
from langgraph.checkpoint.base import BaseCheckpointSaver

from blossom.agent.retention import sweep_saved_state
from blossom.agent.trace import LocalRunTracer
from blossom.clock import Clock, SystemClock, clock_from
from blossom.settings import Settings, enforce_local_only_tracing
from blossom.sources import FixtureSource
from blossom.stores.checkpoints import open_checkpointer
from blossom.stores.drafts import DraftsStore
from blossom.stores.household_claim import claim_household
from blossom.stores.project_state import ProjectStateStore
from blossom.stores.reflections import ReflectionsStore
from blossom.stores.support_rules import SupportRulesStore
from blossom.stores.traces import TraceStore
from blossom.stores.workload_signals import WorkloadSignalsStore

logger = logging.getLogger(__name__)

SWEEP_INTERVAL_SECONDS: Final = 60 * 60
"""How often a running process sweeps what has aged out. Retention is stated in
days, so an hour is often enough for a signal's week or a draft's fortnight to
end within the hour it ends, and rare enough to cost nothing."""

Lifespan = Callable[[FastAPI], AbstractAsyncContextManager[None]]

STATE_ATTRIBUTE = "blossom_state"


@dataclass(frozen=True)
class ApplicationState:
    """Everything a request handler may need, assembled once."""

    settings: Settings
    clock: Clock
    source: FixtureSource
    project_state: ProjectStateStore
    support_rules: SupportRulesStore
    reflections: ReflectionsStore
    drafts: DraftsStore
    """The record of every draft and decision, in the file at
    ``BLOSSOM_DATABASE_PATH``. Durable on purpose: the parent's queue has to
    survive a restart, and the saved-state store answers questions about one
    thread, not across them."""
    checkpointer: BaseCheckpointSaver[str]
    """Where a graph's state and pauses are persisted. Opened and closed by the
    lifespan around this object, so ``close`` does not touch it."""
    traces: TraceStore
    """The framework's trace of every run, in the file at ``BLOSSOM_TRACE_PATH``,
    kept for two weeks."""
    tracer: LocalRunTracer
    """The callback that writes each run's tree to ``traces``. Attached to every
    run the routes start or resume; never saved with the run."""
    workload_signals: WorkloadSignalsStore
    """Her signals that a day is too much, in the drafts file, kept for a week."""
    in_flight: set[str] = field(default_factory=set)
    """The threads of runs this process is running right now, from the moment a
    run starts to the moment it pauses or ends. The scheduled sweep leaves them
    alone: a run between saving its draft and pausing with it looks, from the
    tables, like a run that died there, and only the process running it can
    tell the difference, which is why one process serves a household and says
    so by claiming its files at startup. A run joins the set under the decision
    lock, so it starts either before a sweep or after one, never during its
    count of threads. Empty at startup, when nothing is in flight."""
    decision_lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    """Held while a decision is checked against the table and carried into the
    paused thread, so two decisions about one draft cannot both pass the check.
    Also held while her signal is recorded or taken back, while a run starts
    and while it publishes its draft, and while the scheduled sweep runs, since
    a decision is checked against the evening as signaled and that must not
    change before the decision lands, and a sweep must see every run that is in
    flight. One lock for all of it: each section is short and none of them is
    frequent. It serializes within this process, and one process serves a
    household, held to that by the claim on its files taken at startup; the
    table's own refusal of a second, different decision stands as a backstop
    all the same."""

    def close(self) -> None:
        """Release resources held for the lifetime of the application."""
        self.project_state.close()
        self.drafts.close()
        self.traces.close()
        self.workload_signals.close()


def build_application_state(
    settings: Settings, checkpointer: BaseCheckpointSaver[str]
) -> ApplicationState:
    """Open the stores and seed them from the configured fixture set.

    The project state store is in memory; choosing when project state becomes
    durable is a design decision rather than a wiring detail. The drafts store
    is a file, at ``BLOSSOM_DATABASE_PATH``, because a queue that forgets its
    contents at restart is not a record. The checkpointer is passed in because
    it must be opened inside a running event loop, which only the lifespan has.
    """
    clock = clock_from(settings.today, settings.timezone_key)
    connection = sqlite3.connect(":memory:", check_same_thread=False)
    project_state = ProjectStateStore(connection, clock=clock)
    opened: list[ProjectStateStore | DraftsStore | TraceStore | WorkloadSignalsStore] = [
        project_state
    ]
    # A later step can refuse its path or fail to open its file. Whatever was
    # opened before it is closed on the way out, so a startup that fails and is
    # retried leaves no connection behind.
    try:
        source = FixtureSource(settings.fixture_path)
        project_state.upsert_assignments(source.assignments())
        support_rules = SupportRulesStore()
        for rule in source.support_rules():
            support_rules.add_rule(rule)
        reflections = ReflectionsStore()
        for note in source.reflections():
            reflections.write(note)
        drafts = DraftsStore.open(settings.database_path, clock)
        opened.append(drafts)
        # Retention runs on the real clock even when the household clock is
        # pinned for the fixtures: a pinned clock would stamp every trace with
        # the same day and never move the cutoff, so nothing would age out.
        traces = TraceStore.open(settings.trace_path, SystemClock(clock.zone))
        opened.append(traces)
        traces.sweep()
        # Her signals share the drafts file and, like the trace, are stamped and
        # swept by the real clock; which evening a signal is about comes from
        # the household clock when it is recorded.
        signals = WorkloadSignalsStore.open(settings.database_path, SystemClock(clock.zone))
        opened.append(signals)
        signals.sweep()
    except Exception:
        for store in reversed(opened):
            store.close()
        raise
    return ApplicationState(
        settings=settings,
        clock=clock,
        source=source,
        project_state=project_state,
        support_rules=support_rules,
        reflections=reflections,
        drafts=drafts,
        checkpointer=checkpointer,
        traces=traces,
        tracer=LocalRunTracer(traces),
        workload_signals=signals,
    )


async def sweep_aged(state: ApplicationState) -> None:
    """Apply every retention rule once: expired drafts, old traces, old signals.

    The saved-state sweep records decisions, so it runs under the decision
    lock like any other decision. The other two delete rows nothing reads any
    more, since both stores already leave aged rows out of every read.
    """
    async with state.decision_lock:
        await sweep_saved_state(
            state.checkpointer, state.drafts, state.clock, in_flight=state.in_flight
        )
    state.traces.sweep()
    state.workload_signals.sweep()


async def repeat(interval: float, tick: Callable[[], Awaitable[None]]) -> None:
    """Run ``tick`` every ``interval`` seconds until the task is canceled.

    A tick that fails is logged and the schedule goes on, because a sweep that
    fails once is a reason to look, not a reason to stop keeping the rules.
    """
    while True:
        await asyncio.sleep(interval)
        try:
            await tick()
        except Exception:
            logger.exception("a scheduled sweep failed; the next one runs in %s seconds", interval)


def create_lifespan(settings: Settings) -> Lifespan:
    """Build the lifespan handler that owns application state for one process."""

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        # Startup, not import, is where the process environment may be
        # changed: hosted tracing is forced off here, before any store or
        # model client exists that could read the old value.
        enforce_local_only_tracing()
        # One process serves a household. The claim is taken before any file
        # is opened, so a second process is refused with a sentence rather
        # than left to share state it would then corrupt, and released when
        # this process stops.
        claim = claim_household(settings.database_path)
        try:
            async with open_checkpointer(settings.checkpoint_path) as checkpointer:
                state = build_application_state(settings, checkpointer)
                sweeper: asyncio.Task[None] | None = None
                try:
                    # Whatever the last process left behind: finished threads never
                    # cleared, runs that never finished, drafts that waited too long.
                    # Inside the block, so a sweep that fails still closes the stores.
                    await sweep_saved_state(checkpointer, state.drafts, state.clock)
                    setattr(app.state, STATE_ATTRIBUTE, state)
                    # The same rules on a schedule, so a process that outlives a
                    # signal's week or a draft's fortnight keeps them without a restart.
                    sweeper = asyncio.create_task(
                        repeat(SWEEP_INTERVAL_SECONDS, lambda: sweep_aged(state))
                    )
                    yield
                finally:
                    if sweeper is not None:
                        # Wait for the task to end; what ended it is not raised here.
                        sweeper.cancel()
                        await asyncio.wait([sweeper])
                    state.close()
        finally:
            claim.release()

    return lifespan


def get_application_state(request: Request) -> ApplicationState:
    """FastAPI dependency returning the state built at startup.

    Override this in tests with ``app.dependency_overrides`` to substitute a
    different set of stores.
    """
    state = getattr(request.app.state, STATE_ATTRIBUTE, None)
    if state is None:
        msg = (
            "application state is missing; the app was used without running its "
            "lifespan. Use `with TestClient(app) as client:` rather than "
            "`TestClient(app)`."
        )
        raise RuntimeError(msg)
    return cast(ApplicationState, state)
