# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Gerardo Bodegas Martinez
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
import secrets
import time
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import AbstractAsyncContextManager, asynccontextmanager
from dataclasses import dataclass, field
from typing import Any, Final, cast

from fastapi import FastAPI, Request
from langgraph.checkpoint.base import BaseCheckpointSaver

from blossom.agent.retention import sweep_saved_state
from blossom.agent.trace import LocalRunTracer
from blossom.clock import Clock, SystemClock, clock_from
from blossom.grades.identity import name_form_key
from blossom.household import SignInAttempts, keys_for, secret_beside
from blossom.routes.forms import quiet_the_parser
from blossom.sample_notes import plant_notes
from blossom.settings import Settings, enforce_local_only_tracing
from blossom.sources import FixtureSource, read_whole
from blossom.stores.checkpoints import open_checkpointer
from blossom.stores.drafts import DraftsStore
from blossom.stores.help_requests import HelpRequestsStore
from blossom.stores.household_claim import claim_household
from blossom.stores.paths import refuse_mismarked_state
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
    project_state: ProjectStateStore
    """Assignments and every channel's claim about their dates, in the file at
    ``BLOSSOM_DATABASE_PATH``. Read from a fixture only when one is named and
    the file is blank; what the family enters is never written over."""
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
    help_requests: HelpRequestsStore
    """Her requests for help and what a parent did with each, in the drafts file,
    kept until resolved and for two weeks after."""
    real_clock: Clock
    """The real clock, whatever day the household's is pinned to: the traces, her signals and
    her requests are stamped and swept by it, and a page's forms carry the instant it issued
    them, so a form's age is real time."""
    monotonic: Callable[[], float] = time.monotonic
    """The process's one monotonic clock: every run's time limit and the drafts
    store's deadline checks read it, so a test that moves it moves both."""
    detached: set["asyncio.Future[Any]"] = field(default_factory=set)
    """Work a request stopped waiting for, such as a plan graph still unwinding, held
    only so it is not lost before it ends. Nothing reads it to decide anything; each
    piece leaves the set as it ends."""
    decision_lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    """Held while a decision is checked against the table and carried into the
    paused thread, so two decisions about one draft cannot both pass the check.
    Also held while her signal is recorded or taken back, while a run publishes
    its draft, and while the scheduled sweep runs, since a decision is checked
    against the evening as signaled and that must not change before the
    decision lands. One lock for all of it: each section is short and none of
    them is frequent. It serializes within this process, and one process serves
    a household, held to that by the claim on its files taken at startup; the
    table's own refusal of a second, different decision stands as a backstop
    all the same."""
    attempts: SignInAttempts = field(default_factory=SignInAttempts)
    """Wrong passphrases counted per device, so guessing is slowed; empty at every start."""
    result_key: bytes = field(default_factory=lambda: secrets.token_bytes(32))
    """The key that signs what a save carries to the page it returns to, and the answers a
    paste review was made with, drawn afresh at every start and never written anywhere: a
    result worked out from the address, or one signed before a restart, says nothing."""

    def close(self) -> None:
        """Release resources held for the lifetime of the application."""
        self.project_state.close()
        self.drafts.close()
        self.traces.close()
        self.workload_signals.close()
        self.help_requests.close()


def build_application_state(
    settings: Settings,
    checkpointer: BaseCheckpointSaver[str],
    monotonic: Callable[[], float] = time.monotonic,
    real_clock: Clock | None = None,
) -> ApplicationState:
    """Open the stores, and seed the record from a fixture when one is named and it is empty.

    Project state and the drafts are one file, at ``BLOSSOM_DATABASE_PATH``,
    because a record that forgets its assignments at restart is not a record,
    and neither is a queue that forgets its contents. The planner's rules and
    notes are read from the fixture at every start, and are empty without
    one. The checkpointer is passed in because it must be opened inside a
    running event loop, which only the lifespan has. ``monotonic`` is the clock
    every run's time limit is read from, the drafts store's included.
    """
    clock = clock_from(settings.today, settings.timezone_key)
    real = SystemClock(clock.zone) if real_clock is None else real_clock
    fixture = None if settings.fixture_path is None else FixtureSource(settings.fixture_path)
    # A fixture is read only into a blank file, in one transaction with the
    # file's tables. A file with anything in it is the household's record,
    # whatever it holds, and is left alone: what the family enters outlives a
    # restart, a sample edited by hand stays edited until its folder is
    # deleted, and a household file from before the record lived in it is not
    # seeded by a fixture path left in .env.
    project_state = ProjectStateStore.initialize(
        settings.database_path,
        clock,
        seed=None if fixture is None else lambda: read_whole(fixture),
        notes=None
        if fixture is None
        else lambda store: plant_notes(store, fixture.homework_notes()),
    )
    opened: list[
        ProjectStateStore | DraftsStore | TraceStore | WorkloadSignalsStore | HelpRequestsStore
    ] = [project_state]
    # A later step can refuse its path or fail to open its file. Whatever was
    # opened before it is closed on the way out, so a startup that fails and is
    # retried leaves no connection behind.
    try:
        support_rules = SupportRulesStore()
        reflections = ReflectionsStore()
        if fixture is not None:
            for rule in fixture.support_rules():
                support_rules.add_rule(rule)
            for note in fixture.reflections():
                reflections.write(note)
        drafts = DraftsStore.open(settings.database_path, clock, monotonic=monotonic)
        opened.append(drafts)
        # Retention runs on the real clock even when the household clock is
        # pinned for the fixtures: a pinned clock would stamp every trace with
        # the same day and never move the cutoff, so nothing would age out.
        traces = TraceStore.open(settings.trace_path, real)
        opened.append(traces)
        traces.sweep()
        # Her signals share the drafts file and, like the trace, are stamped and
        # swept by the real clock; which evening a signal is about comes from
        # the household clock when it is recorded.
        signals = WorkloadSignalsStore.open(settings.database_path, real)
        opened.append(signals)
        signals.sweep()
        # Her requests for help are stamped and swept the same way.
        help_requests = HelpRequestsStore.open(settings.database_path, real)
        opened.append(help_requests)
        help_requests.sweep()
    except Exception:
        for store in reversed(opened[1:]):
            store.close()
        # A file that was blank at this start is removed with it, so a start
        # that fails partway leaves nothing behind and the next start begins afresh.
        project_state.discard_if_new()
        raise
    return ApplicationState(
        settings=settings,
        clock=clock,
        project_state=project_state,
        support_rules=support_rules,
        reflections=reflections,
        drafts=drafts,
        checkpointer=checkpointer,
        traces=traces,
        tracer=LocalRunTracer(traces),
        workload_signals=signals,
        help_requests=help_requests,
        real_clock=real,
        monotonic=monotonic,
    )


async def sweep_aged(state: ApplicationState) -> None:
    """Apply every retention rule once: expired drafts, old traces, old signals.

    The saved-state sweep records decisions, so it runs under the decision
    lock like any other decision. The other two delete rows nothing reads any
    more, since both stores already leave aged rows out of every read. Each
    store waits for its file on a worker thread, so a held file never holds
    the server.
    """
    async with state.decision_lock:
        await sweep_saved_state(state.checkpointer, state.drafts, state.clock)
    await asyncio.to_thread(state.traces.sweep)
    await asyncio.to_thread(state.workload_signals.sweep)
    await asyncio.to_thread(state.help_requests.sweep)


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


def create_lifespan(
    settings: Settings,
    monotonic: Callable[[], float] = time.monotonic,
    real_clock: Clock | None = None,
) -> Lifespan:
    """Build the lifespan handler that owns application state for one process, its runs
    timed on ``monotonic`` and its real time read from ``real_clock``, the system's unless
    given."""

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        # Startup, not import, is where the process environment may be
        # changed: hosted tracing is forced off here, before any store or
        # model client exists that could read the old value.
        enforce_local_only_tracing()
        # The form parser's own log lines can quote a body it refuses; the application
        # logs each refusal by its kind, so the parser's lines stay out of the log.
        quiet_the_parser()
        # One process serves a household. The claim covers both files that
        # make up its state, is taken before either is opened, so a second
        # process is refused with a sentence rather than left to share state
        # it would then corrupt, and is released when this process stops.
        # First, a test copy is held to what it declares: it runs only where its folders are
        # marked and its files are its own, never links, and a marked folder runs only as a
        # copy. Nothing is claimed or opened until this passes.
        refuse_mismarked_state(
            settings.database_path,
            settings.checkpoint_path,
            settings.trace_path,
            test_copy=settings.test_copy,
        )
        claim = claim_household(settings.database_path, settings.checkpoint_path)
        try:
            async with open_checkpointer(settings.checkpoint_path) as checkpointer:
                state = build_application_state(settings, checkpointer, monotonic, real_clock)
                sweeper: asyncio.Task[None] | None = None
                try:
                    # A run the record holds as running belongs to a process that
                    # stopped, and its deadline was read on that process's clock: it
                    # ends interrupted before anything is served. A failure here stops
                    # the start, as a failed sweep does, since such a row would refuse
                    # every press until long after its evening.
                    await asyncio.to_thread(state.drafts.end_interrupted_runs)
                    # Whatever the last process left behind: finished threads never
                    # cleared, drafts that waited too long. Inside the block, so a
                    # sweep that fails still closes the stores.
                    await sweep_saved_state(checkpointer, state.drafts, state.clock)
                    setattr(app.state, STATE_ATTRIBUTE, state)
                    # The key that name forms are made under comes from the same read of
                    # the secret as the sign-in keys. With sign-in off, the first grade review
                    # reads it, so a household that never adds a report never makes one.
                    app.state.grade_name_key = None
                    if settings.household_sign_in:
                        # A key per person, drawn from the secret kept beside the
                        # database and that person's passphrase: a restart keeps
                        # everyone signed in, a changed passphrase signs one person out.
                        secret = secret_beside(settings.database_path)
                        app.state.household_keys = keys_for(secret, settings)
                        app.state.grade_name_key = name_form_key(secret)
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
