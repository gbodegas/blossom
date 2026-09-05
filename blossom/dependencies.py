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

import sqlite3
from collections.abc import AsyncIterator, Callable
from contextlib import AbstractAsyncContextManager, asynccontextmanager
from dataclasses import dataclass
from typing import cast

from fastapi import FastAPI, Request
from langgraph.checkpoint.base import BaseCheckpointSaver

from blossom.clock import Clock, clock_from
from blossom.settings import Settings, enforce_local_only_tracing
from blossom.sources import FixtureSource
from blossom.stores.checkpoints import open_checkpointer
from blossom.stores.drafts import DraftsStore
from blossom.stores.project_state import ProjectStateStore
from blossom.stores.reflections import ReflectionsStore
from blossom.stores.support_rules import SupportRulesStore

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

    def close(self) -> None:
        """Release resources held for the lifetime of the application."""
        self.project_state.close()
        self.drafts.close()


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
    source = FixtureSource(settings.fixture_path)
    project_state.upsert_assignments(source.assignments())
    return ApplicationState(
        settings=settings,
        clock=clock,
        source=source,
        project_state=project_state,
        # Empty until seed data exists. The graph reads whichever rules and
        # notes are here, so an empty store means a plan built without them.
        support_rules=SupportRulesStore(),
        reflections=ReflectionsStore(),
        drafts=DraftsStore.open(settings.database_path, clock),
        checkpointer=checkpointer,
    )


def create_lifespan(settings: Settings) -> Lifespan:
    """Build the lifespan handler that owns application state for one process."""

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        # Startup, not import, is where the process environment may be
        # changed: hosted tracing is forced off here, before any store or
        # model client exists that could read the old value.
        enforce_local_only_tracing()
        async with open_checkpointer(settings.checkpoint_path) as checkpointer:
            state = build_application_state(settings, checkpointer)
            setattr(app.state, STATE_ATTRIBUTE, state)
            try:
                yield
            finally:
                state.close()

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
