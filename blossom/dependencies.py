"""Application-scoped objects, built once at startup and injected per request.

Construction happens once, in the application lifespan, and routes receive
what they need through ``Depends``. Building stores inside a request handler
would open a connection and re-seed fixtures on every call, and every new
route would copy the pattern. The lifespan also gives tests a seam: overriding
``get_application_state`` swaps the whole backing world without touching the
environment or the filesystem.

On thread safety. FastAPI runs synchronous path operations in a worker thread
pool, so a connection created once at startup is used from several threads.
``sqlite3`` forbids that by default, which is why the connection is opened with
``check_same_thread=False`` and why ``ProjectStateStore`` serializes access with
a lock.
"""

import sqlite3
from collections.abc import AsyncIterator, Callable
from contextlib import AbstractAsyncContextManager, asynccontextmanager
from dataclasses import dataclass
from typing import cast

from fastapi import FastAPI, Request

from blossom.clock import Clock, clock_from
from blossom.settings import Settings
from blossom.sources import FixtureSource
from blossom.stores.project_state import ProjectStateStore

Lifespan = Callable[[FastAPI], AbstractAsyncContextManager[None]]

STATE_ATTRIBUTE = "blossom_state"


@dataclass(frozen=True)
class ApplicationState:
    """Everything a request handler may need, assembled once."""

    settings: Settings
    clock: Clock
    source: FixtureSource
    project_state: ProjectStateStore

    def close(self) -> None:
        """Release resources held for the lifetime of the application."""
        self.project_state.close()


def build_application_state(settings: Settings) -> ApplicationState:
    """Open the stores and seed them from the configured fixture set.

    The project state store is in memory. ``BLOSSOM_DATABASE_PATH`` is read
    into settings but not used here: choosing when state becomes durable is a
    design decision rather than a wiring detail.
    """
    clock = clock_from(settings.today)
    connection = sqlite3.connect(":memory:", check_same_thread=False)
    project_state = ProjectStateStore(connection, clock=clock)
    source = FixtureSource(settings.fixture_path)
    project_state.upsert_assignments(source.assignments())
    return ApplicationState(
        settings=settings,
        clock=clock,
        source=source,
        project_state=project_state,
    )


def create_lifespan(settings: Settings) -> Lifespan:
    """Build the lifespan handler that owns application state for one process."""

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        state = build_application_state(settings)
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
