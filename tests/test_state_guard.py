"""The suite cannot open a household's files, and says so before it touches them.

Every case here names a protected place and watches what would reach it. The
watch refuses as well as records, so a guard that stopped working would fail
these tests without the checkout's own state folder, or anything standing in
for a household's, being written.
"""

import asyncio
import ctypes
import os
import pathlib
import sqlite3
import subprocess
import sys
from collections.abc import Callable

import pytest
from fastapi.testclient import TestClient

from blossom import app as app_module
from blossom import settings as settings_module
from blossom.app import create_app
from blossom.household import secret_beside
from blossom.settings import (
    CHECKPOINT_PATH_VARIABLE,
    DATABASE_PATH_VARIABLE,
    TRACE_PATH_VARIABLE,
    Settings,
    get_settings,
)
from blossom.stores.checkpoints import open_checkpointer
from blossom.stores.drafts import DraftsStore
from blossom.stores.help_requests import HelpRequestsStore
from blossom.stores.household_claim import claim_household
from blossom.stores.paths import UnsafeCheckpointPath, refuse_unsafe_path
from blossom.stores.project_state import ProjectStateStore
from blossom.stores.traces import TraceStore
from blossom.stores.workload_signals import WorkloadSignalsStore
from tests.state_guard import (
    CHECKOUT_STATE,
    RUNTIME_PATH_VARIABLES,
    HouseholdStateProtected,
    StateGuard,
    lands_inside,
)
from tests.support import fixture_clock, fixture_settings

REPOSITORY = pathlib.Path(__file__).resolve().parent.parent
WINDOWS_ONLY = pytest.mark.skipif(os.name != "nt", reason="a Windows spelling of a path")


class Touches:
    """What reached a watched folder: made a directory, opened a file, or connected."""

    def __init__(self) -> None:
        self.watched: list[pathlib.Path] = []
        self.seen: list[tuple[str, str]] = []

    def watch(self, folder: pathlib.Path) -> None:
        """Record, and refuse, whatever reaches ``folder`` from here on."""
        self.watched.append(folder)

    def reached(self, how: str, target: object) -> None:
        """Note one attempt, and stop it, when it lands in a watched folder."""
        if not isinstance(target, str | os.PathLike):
            return
        path = pathlib.Path(os.fspath(target))
        if any(lands_inside(path, folder) for folder in self.watched):
            self.seen.append((how, str(path)))
            msg = f"{how} reached {path}, which this test watches"
            raise AssertionError(msg)


@pytest.fixture
def touches(monkeypatch: pytest.MonkeyPatch) -> Touches:
    """Spies on the three ways a start reaches the disk: a folder, a file, a database.

    The claim opens its lock file through ``Path.open``, the sign-in secret
    through ``os.open``, and every store, the trace file and the saved-state
    file included, through ``sqlite3.connect``, which is also what the
    asynchronous driver calls on its own thread.
    """
    touches = Touches()

    def watched[**P, R](how: str, real: Callable[P, R]) -> Callable[P, R]:
        """The same call, with its first argument, the path, shown to the watch first."""

        def spy(*args: P.args, **kwargs: P.kwargs) -> R:
            touches.reached(how, args[0] if args else None)
            return real(*args, **kwargs)

        return spy

    monkeypatch.setattr(pathlib.Path, "mkdir", watched("mkdir", pathlib.Path.mkdir))
    monkeypatch.setattr(pathlib.Path, "open", watched("open", pathlib.Path.open))
    monkeypatch.setattr(os, "open", watched("os.open", os.open))
    monkeypatch.setattr(sqlite3, "connect", watched("sqlite3.connect", sqlite3.connect))
    return touches


def start(settings: Settings | None = None) -> None:
    """Start the application and stop it again."""
    with TestClient(create_app(settings)):
        pass


def test_the_protected_folder_is_the_checkouts_own_whatever_the_default_says(
    tmp_path: pathlib.Path,
) -> None:
    """The per-test fixture moves the default; what is protected does not move with it."""
    assert tmp_path == settings_module.LOCAL_STATE_PATH
    assert lands_inside(REPOSITORY / ".local" / "blossom.sqlite3", CHECKOUT_STATE)
    assert not lands_inside(tmp_path / "blossom.sqlite3", CHECKOUT_STATE)


def test_a_start_on_the_checkouts_defaults_is_refused_and_touches_nothing(
    monkeypatch: pytest.MonkeyPatch, touches: Touches
) -> None:
    """What a fixture wider than one test would do: start before the defaults have moved."""
    monkeypatch.setattr(settings_module, "LOCAL_STATE_PATH", REPOSITORY / ".local")
    settings = fixture_settings()
    assert lands_inside(settings.database_path, CHECKOUT_STATE)
    touches.watch(CHECKOUT_STATE)

    with pytest.raises(HouseholdStateProtected) as refusal:
        start(settings)

    assert touches.seen == []
    assert "function-scoped fixture" in str(refusal.value)
    assert str(settings.database_path) in str(refusal.value)


@pytest.mark.parametrize(
    "variable", [DATABASE_PATH_VARIABLE, CHECKPOINT_PATH_VARIABLE, TRACE_PATH_VARIABLE]
)
def test_each_destination_is_checked_before_a_start_creates_anything(
    variable: str, tmp_path: pathlib.Path, state_guard: StateGuard, touches: Touches
) -> None:
    """One protected destination is enough, and the other two are not created first."""
    household = tmp_path / "household"
    settings = fixture_settings(**{variable: str(household / "state.sqlite3")})
    touches.watch(tmp_path)

    with state_guard.also_protecting(household), pytest.raises(HouseholdStateProtected):
        start(settings)

    assert touches.seen == []
    assert list(tmp_path.iterdir()) == []


def open_the_saved_state(path: pathlib.Path) -> None:
    async def opened() -> None:
        async with open_checkpointer(path):
            pass

    asyncio.run(opened())


OPENERS: dict[str, Callable[[pathlib.Path], object]] = {
    "the claim": claim_household,
    "the record": lambda path: ProjectStateStore.initialize(path, fixture_clock()),
    "the drafts": lambda path: DraftsStore.open(path, fixture_clock()),
    "the traces": lambda path: TraceStore.open(path, fixture_clock()),
    "her signals": lambda path: WorkloadSignalsStore.open(path, fixture_clock()),
    "her requests": lambda path: HelpRequestsStore.open(path, fixture_clock()),
    "the saved state": open_the_saved_state,
    "the sign-in secret": secret_beside,
}


@pytest.mark.parametrize("opener", OPENERS.values(), ids=OPENERS.keys())
def test_everything_that_opens_a_state_file_is_refused_first(
    opener: Callable[[pathlib.Path], object],
    tmp_path: pathlib.Path,
    state_guard: StateGuard,
    touches: Touches,
) -> None:
    """A test that skips the application and opens one file directly gets the same answer."""
    household = tmp_path / "household"
    touches.watch(household)

    with state_guard.also_protecting(household), pytest.raises(HouseholdStateProtected):
        opener(household / "blossom.sqlite3")

    assert touches.seen == []
    assert not household.exists()


def short_name_of(folder: pathlib.Path) -> pathlib.Path | None:
    """The 8.3 spelling of an existing folder, when the volume keeps one."""
    windll = getattr(ctypes, "windll", None)
    if windll is None:
        return None
    buffer = ctypes.create_unicode_buffer(1024)
    if not windll.kernel32.GetShortPathNameW(str(folder), buffer, len(buffer)):
        return None
    short = pathlib.Path(buffer.value)
    return None if str(short).lower() == str(folder).lower() else short


def linked_to(folder: pathlib.Path, link: pathlib.Path) -> pathlib.Path | None:
    """A junction on Windows, a symlink elsewhere, or None where neither can be made."""
    try:
        if sys.platform == "win32":
            import _winapi

            _winapi.CreateJunction(str(folder), str(link))
        else:
            link.symlink_to(folder, target_is_directory=True)
    except OSError:
        return None
    return link


def test_a_walk_through_another_folder_is_the_same_place(
    tmp_path: pathlib.Path, state_guard: StateGuard
) -> None:
    household = tmp_path / "household"
    (tmp_path / "elsewhere").mkdir()
    walked = tmp_path / "elsewhere" / ".." / "household" / "blossom.sqlite3"

    with state_guard.also_protecting(household), pytest.raises(HouseholdStateProtected):
        state_guard.refuse(walked)


def test_a_relative_spelling_is_the_same_place(
    tmp_path: pathlib.Path, state_guard: StateGuard, monkeypatch: pytest.MonkeyPatch
) -> None:
    household = tmp_path / "household"
    monkeypatch.chdir(tmp_path)

    with state_guard.also_protecting(household), pytest.raises(HouseholdStateProtected):
        state_guard.refuse(pathlib.Path("household") / "blossom.sqlite3")


@WINDOWS_ONLY
@pytest.mark.parametrize(
    "respell",
    [
        pytest.param(lambda text: text.upper(), id="upper case"),
        pytest.param(lambda text: text.lower(), id="lower case"),
        pytest.param(lambda text: text.replace("\\", "/"), id="forward slashes"),
        pytest.param(lambda text: "\\\\?\\" + text, id="extended-length prefix"),
    ],
)
def test_a_windows_spelling_is_the_same_place(
    respell: Callable[[str], str], tmp_path: pathlib.Path, state_guard: StateGuard
) -> None:
    household = tmp_path / "household"
    spelled = pathlib.Path(respell(str(household / "blossom.sqlite3")))

    with state_guard.also_protecting(household), pytest.raises(HouseholdStateProtected):
        state_guard.refuse(spelled)


@WINDOWS_ONLY
def test_a_short_name_is_the_same_place(tmp_path: pathlib.Path, state_guard: StateGuard) -> None:
    household = tmp_path / "household state folder"
    household.mkdir()
    short = short_name_of(household)
    if short is None:
        pytest.skip("this volume keeps no 8.3 names")

    with state_guard.also_protecting(household), pytest.raises(HouseholdStateProtected):
        state_guard.refuse(short / "blossom.sqlite3")


def test_a_link_to_the_folder_is_the_same_place(
    tmp_path: pathlib.Path, state_guard: StateGuard
) -> None:
    household = tmp_path / "household"
    household.mkdir()
    link = linked_to(household, tmp_path / "plainly-named")
    if link is None:
        pytest.skip("no junction or symlink can be made here")

    with state_guard.also_protecting(household), pytest.raises(HouseholdStateProtected):
        state_guard.refuse(link / "blossom.sqlite3")


def test_a_neighbor_that_starts_with_the_same_name_is_left_alone(
    tmp_path: pathlib.Path, state_guard: StateGuard
) -> None:
    household = tmp_path / "household"

    with state_guard.also_protecting(household):
        state_guard.refuse(tmp_path / "household-notes" / "blossom.sqlite3")
        state_guard.refuse(tmp_path / "blossom.sqlite3")


def test_the_folder_an_inherited_variable_pointed_into_is_protected(
    tmp_path: pathlib.Path,
) -> None:
    """The claim's lock file and the sign-in secret sit beside the file the variable names."""
    household = tmp_path / "household"
    inherited = {
        DATABASE_PATH_VARIABLE: str(household / "blossom.sqlite3"),
        TRACE_PATH_VARIABLE: " ",
    }
    guard = StateGuard(checkout_state=(), inherited=inherited)

    for beside in ("blossom.sqlite3", "blossom.lock", "checkpoints.sqlite3"):
        with pytest.raises(HouseholdStateProtected) as refusal:
            guard.refuse(household / beside)
        assert DATABASE_PATH_VARIABLE in str(refusal.value)
    guard.refuse(tmp_path / "blossom.sqlite3")
    assert list(guard.inherited) == [DATABASE_PATH_VARIABLE]


def test_no_runtime_path_is_inherited_by_a_test() -> None:
    assert [variable for variable in RUNTIME_PATH_VARIABLES if variable in os.environ] == []


def test_a_run_that_inherits_a_household_path_never_opens_it(tmp_path: pathlib.Path) -> None:
    """The whole arrangement, in a run of its own that inherits all three variables."""
    household = tmp_path / "household"
    environ = {
        **os.environ,
        DATABASE_PATH_VARIABLE: str(household / "blossom.sqlite3"),
        CHECKPOINT_PATH_VARIABLE: str(household / "checkpoints.sqlite3"),
        TRACE_PATH_VARIABLE: str(household / "traces.sqlite3"),
    }

    inner = ["tests/inherited_run_cases.py", "-q", "-p", "no:cacheprovider"]
    run = subprocess.run(  # noqa: S603
        [sys.executable, "-m", "pytest", *inner, "--basetemp", str(tmp_path / "inner")],
        cwd=REPOSITORY,
        env=environ,
        capture_output=True,
        text=True,
        check=False,
    )

    assert run.returncode == 0, run.stdout + run.stderr
    assert "4 passed" in run.stdout
    assert not household.exists()


def test_the_application_built_at_import_is_refused_at_its_claim(
    state_guard: StateGuard, touches: Touches
) -> None:
    """``blossom.app.app`` exists before the guard does, on settings read at import.

    Its start is the application's own, with no check of all three destinations
    first. Its database is the default or the one the shell named, a protected
    folder either way, and the claim on it is the first thing a start does.
    """
    touches.watch(CHECKOUT_STATE)
    for folder in state_guard.inherited.values():
        touches.watch(folder)

    with pytest.raises(HouseholdStateProtected), TestClient(app_module.app):
        pass

    assert touches.seen == []


def test_settings_cached_from_the_environment_are_refused_like_any_other(
    tmp_path: pathlib.Path,
    state_guard: StateGuard,
    touches: Touches,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An application built with no settings of its own reads the cached ones."""
    household = tmp_path / "household"
    monkeypatch.setenv(DATABASE_PATH_VARIABLE, str(household / "blossom.sqlite3"))
    get_settings.cache_clear()
    assert get_settings().database_path == household / "blossom.sqlite3"
    touches.watch(tmp_path)

    with state_guard.also_protecting(household), pytest.raises(HouseholdStateProtected):
        start()

    assert touches.seen == []


def test_settings_cached_before_a_test_do_not_reach_it(tmp_path: pathlib.Path) -> None:
    """The cache is emptied around every test, so the next read lands in its folder."""
    assert get_settings().database_path == tmp_path / "blossom.sqlite3"
    assert get_settings().checkpoint_path == tmp_path / "checkpoints.sqlite3"
    assert get_settings().trace_path == tmp_path / "traces.sqlite3"


def test_a_start_on_temporary_state_is_untouched_by_the_guard(tmp_path: pathlib.Path) -> None:
    start()

    assert (tmp_path / "blossom.sqlite3").exists()
    assert (tmp_path / "checkpoints.sqlite3").exists()
    assert (tmp_path / "traces.sqlite3").exists()


def test_a_path_the_application_refuses_is_still_refused_in_its_own_words(
    touches: Touches,
) -> None:
    """Examining a rejected path stays what it was: an answer, with nothing opened."""
    touches.watch(CHECKOUT_STATE)

    with pytest.raises(UnsafeCheckpointPath, match="network share"):
        refuse_unsafe_path(pathlib.Path("\\\\server\\share\\blossom.sqlite3"), environ={})
    with pytest.raises(UnsafeCheckpointPath, match="in-memory"):
        refuse_unsafe_path(pathlib.Path(":memory:"), environ={})

    assert touches.seen == []


def test_settings_that_name_a_protected_place_can_be_built_and_read(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Building settings opens nothing, so the settings tests keep their own paths."""
    monkeypatch.setattr(settings_module, "LOCAL_STATE_PATH", REPOSITORY / ".local")

    settings = fixture_settings()

    assert settings.database_path == REPOSITORY / ".local" / "blossom.sqlite3"


def test_a_guard_put_in_place_is_taken_out_again(tmp_path: pathlib.Path) -> None:
    household = tmp_path / "household"
    second = StateGuard(checkout_state=(household,), inherited={})

    with second.installed(), pytest.raises(HouseholdStateProtected):
        claim_household(household / "blossom.sqlite3")

    claim = claim_household(household / "blossom.sqlite3")
    claim.release()
    assert (household / "blossom.lock").exists()
