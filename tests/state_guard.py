"""Keeps the suite away from a household's files.

The per-test fixture moves the default state folder to a temporary one, which
covers a test that starts the application the ordinary way. Two ways past it
remain. A fixture wider than one test runs before that move, so an application
it starts reads the defaults as they are, inside the checkout. And a shell that
ran Blossom hands its ``BLOSSOM_DATABASE_PATH`` to the suite, so settings read
from the environment name the household's own file.

So the run protects two kinds of folder: the checkout's ``.local``, found from
where this file sits rather than from the default a fixture moves, and the
folder each inherited runtime-path variable pointed into, since the claim's
lock file and the sign-in secret sit beside the file a variable names. A path
is compared by where it lands, so another spelling of a protected folder, a
different case, a walk through ``..``, a short name, a link, is the same
folder. Asking where a path lands reads names from the disk and opens nothing.

The refusal sits at the two places everything passes. Every state file, the
claim's lock, and the sign-in secret go through ``refuse_unsafe_path`` before a
folder is made or a file opened, so a path that guard accepts is checked here
next; one it refuses keeps its own error, which the path tests read. And a
start checks all three of its destinations before the first of them is
claimed, through that same pair of guards in that same order, so a
destination that is both protected and refused by the application keeps the
application's error there too. Both are put in place for the run and taken
out after it. Nothing in the application knows any of this exists.

``blossom.app.app`` is built as the application is imported, before any of
this, so its start has no check of all three first. It needs none: its
settings were read at import, its database is the default or the one the
shell named, both protected, and claiming that file is the first thing a
start does.

The shared helpers that prepare settings, open a record, or build the
application are for a run this stands behind, and ask ``protecting`` before
they do anything. Outside one, a script that imports them would build an
application on the defaults, the checkout's own record, with no fixture to
move them. What they ask is whether the path guard and the start every
module holds are the ones put in place here. Nothing else counts: not that
pytest is imported, and not a variable in the environment, either of which a
script can have with no guard anywhere.
"""

import os
import sys
from collections.abc import AsyncIterator, Callable, Iterable, Iterator, Mapping
from contextlib import asynccontextmanager, contextmanager
from pathlib import Path
from typing import Any, Final

from fastapi import FastAPI

from blossom import dependencies
from blossom import settings as settings_module
from blossom.dependencies import Lifespan
from blossom.settings import (
    CHECKPOINT_PATH_VARIABLE,
    DATABASE_PATH_VARIABLE,
    TRACE_PATH_VARIABLE,
    Settings,
    resolve_configured_path,
)
from blossom.stores import paths
from blossom.stores.paths import local_form

RUNTIME_PATH_VARIABLES: Final = (
    DATABASE_PATH_VARIABLE,
    CHECKPOINT_PATH_VARIABLE,
    TRACE_PATH_VARIABLE,
)
CHECKOUT_STATE: Final = Path(__file__).resolve().parent.parent / ".local"
"""The checkout's own state folder, whatever ``LOCAL_STATE_PATH`` says by now."""

DEFAULT_STATE: Final = settings_module.LOCAL_STATE_PATH
"""The default as the application first had it, read before any fixture moves it."""

CHECKOUT_REASON: Final = (
    "this checkout's own state folder. The defaults point there until the per-test fixture "
    "moves them to a temporary folder, so a fixture wider than one test that starts the "
    "application, or settings built before the move, lands here. Start the application "
    "from a function-scoped fixture."
)


class HouseholdStateProtected(RuntimeError):
    """Raised when a test reaches for a folder that may hold a household's files."""


class OutsideTheSuite(RuntimeError):
    """Raised when a shared test helper is used where no guard stands behind the application."""


_STANDING: list[tuple[Callable[..., Any], Callable[..., Any]]] = []
"""The path guard and the start each guard now in place put there, innermost last."""


def protecting() -> bool:
    """True while a guard stands behind the application in this process.

    Read from what is really bound: the path guard the stores call and the
    start the application is built with must be a pair one guard put in
    place and has not yet taken out. A flag would only say that someone
    meant to protect the run."""
    return any(
        paths.refuse_unsafe_path is guard and dependencies.create_lifespan is start
        for guard, start in _STANDING
    )


def require_protection(helper: str) -> None:
    """Refuse a shared helper outside a protected run, before it builds or opens anything."""
    if protecting():
        return
    variables = ", ".join(RUNTIME_PATH_VARIABLES)
    msg = (
        f"tests.support.{helper} is for a test the state guard stands behind, and none does "
        "here, so what it builds would open the checkout's own record. Write this as a test, "
        f"or run the application itself with {variables} each naming a file in a folder made "
        "to be thrown away."
    )
    raise OutsideTheSuite(msg)


def landed(path: Path) -> str:
    """Where a path really is, in the one spelling this platform compares by."""
    return os.path.normcase(local_form(path))


def lands_inside(path: Path, folder: Path) -> bool:
    """True when ``path`` is ``folder`` or anywhere under it, however either is spelled."""
    here, root = landed(path), landed(folder)
    return here == root or here.startswith(root.rstrip(os.sep) + os.sep)


def rebind(original: Callable[..., Any], replacement: Callable[..., Any]) -> None:
    """Point every module that holds ``original``, under any name, at ``replacement``.

    The stores import the path guard by name, so replacing it where it is
    defined would leave each of them holding the one it imported.
    """
    for module in list(sys.modules.values()):
        held = getattr(module, "__dict__", None)
        if not isinstance(held, dict):
            continue
        for name in [name for name, value in held.items() if value is original]:
            setattr(module, name, replacement)


class StateGuard:
    """The folders a test may not open, and the refusal."""

    def __init__(
        self,
        checkout_state: Iterable[Path] = (CHECKOUT_STATE, DEFAULT_STATE),
        inherited: Mapping[str, str] | None = None,
    ) -> None:
        self.inherited: dict[str, Path] = {}
        """The folder each runtime-path variable pointed into when the run began."""
        self._protected: list[tuple[Path, str]] = [
            (folder, CHECKOUT_REASON) for folder in dict.fromkeys(checkout_state)
        ]
        for variable in RUNTIME_PATH_VARIABLES:
            value = (os.environ if inherited is None else inherited).get(variable)
            if value is None or not value.strip():
                continue
            folder = resolve_configured_path(value).parent
            self.inherited[variable] = folder
            reason = (
                f"where {variable} pointed when the run began: a household's files. "
                "The suite runs on temporary state."
            )
            self._protected.append((folder, reason))

    def refuse(self, path: Path) -> None:
        """Raise when ``path`` lands in a protected folder; asking opens nothing."""
        for folder, reason in self._protected:
            if lands_inside(path, folder):
                msg = f"a test may not open {path}: it is inside {folder}, {reason}"
                raise HouseholdStateProtected(msg)

    @contextmanager
    def also_protecting(self, folder: Path) -> Iterator[None]:
        """Treat ``folder`` as a household's for one test, so none has to name a real one."""
        entry = (folder, "which this test protects.")
        self._protected.append(entry)
        try:
            yield
        finally:
            self._protected.remove(entry)

    @contextmanager
    def installed(self) -> Iterator[None]:
        """Stand behind the application's path guard, and before a start, until the block ends."""
        accepts = paths.refuse_unsafe_path
        lifespan_for = dependencies.create_lifespan

        def refuse_unsafe_path(path: Path, environ: Mapping[str, str] | None = None) -> Path:
            safe = accepts(path, environ)
            self.refuse(safe)
            return safe

        def create_lifespan(settings: Settings) -> Lifespan:
            starts = lifespan_for(settings)

            @asynccontextmanager
            async def lifespan(app: FastAPI) -> AsyncIterator[None]:
                # Every file a start would create, before it creates the first,
                # each through the application's guard and then this one, in
                # the order a start reaches them.
                for path in (settings.database_path, settings.checkpoint_path, settings.trace_path):
                    refuse_unsafe_path(path)
                async with starts(app):
                    yield

            return lifespan

        rebind(accepts, refuse_unsafe_path)
        rebind(lifespan_for, create_lifespan)
        standing = (refuse_unsafe_path, create_lifespan)
        _STANDING.append(standing)
        try:
            yield
        finally:
            _STANDING.remove(standing)
            rebind(refuse_unsafe_path, accepts)
            rebind(create_lifespan, lifespan_for)
