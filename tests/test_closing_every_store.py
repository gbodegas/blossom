# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Gerardo Bodegas Martinez
"""Tests for closing the stores when the application stops.

The close of every store is attempted, in one fixed order, whether the closes before it
returned or raised an ordinary error. Each failure is logged with its store's name, and the
first is raised once the last close has been attempted. A close that never returns, and a
process that is ended, are outside what these tests cover.
"""

import asyncio
import logging
import pathlib
import sqlite3
from collections.abc import Callable, Iterator
from typing import Final

import pytest
from fastapi.testclient import TestClient
from langgraph.checkpoint.memory import InMemorySaver
from langgraph.checkpoint.sqlite.aio import AsyncSqliteSaver

from blossom.app import create_app
from blossom.dependencies import ApplicationState, build_application_state
from blossom.stores.drafts import StoreBusy
from blossom.stores.household_claim import AnotherProcessHasTheHousehold, claim_household
from tests.support import SAME_ORIGIN, files_in, fixture_settings, state_of

STORES: Final = (
    "project_state",
    "drafts",
    "traces",
    "workload_signals",
    "help_requests",
    "import_drafts",
)
"""The stores the application closes when it stops, in the order it closes them."""

NAMED: Final = {
    "project_state": "project state",
    "drafts": "drafts",
    "traces": "traces",
    "workload_signals": "workload signals",
    "help_requests": "help requests",
    "import_drafts": "import drafts",
}
"""What the log calls each store."""


def could_not_close(store: str) -> str:
    """The line logged when the close of ``store`` raises."""
    return f"the {NAMED[store]} store could not be closed"


def closed(state: ApplicationState) -> set[str]:
    """The stores of ``state`` whose connection refuses a statement."""
    refusing = set()
    for name in STORES:
        connection: sqlite3.Connection = getattr(state, name)._connection
        try:
            connection.execute("SELECT 1")
        except sqlite3.ProgrammingError:
            refusing.add(name)
    return refusing


def failures_logged(caplog: pytest.LogCaptureFixture) -> list[tuple[str, BaseException | None]]:
    """Each error the application's own module logged: its line and the error it carries."""
    return [
        (record.getMessage(), record.exc_info[1] if record.exc_info else None)
        for record in caplog.records
        if record.name == "blossom.dependencies" and record.levelno == logging.ERROR
    ]


def fail_to_close(
    monkeypatch: pytest.MonkeyPatch, state: ApplicationState, store: str, error: BaseException
) -> None:
    """Make the close of ``store`` raise ``error`` and close nothing."""

    def refuse() -> None:
        raise error

    monkeypatch.setattr(getattr(state, store), "close", refuse)


def remembering(seen: list[str], name: str, close: Callable[[], None]) -> Callable[[], None]:
    """``close``, with ``name`` added to ``seen`` as it is called."""

    def noted() -> None:
        seen.append(name)
        close()

    return noted


async def read_saved_state(saver: AsyncSqliteSaver) -> None:
    """One statement on the saved-state store's connection."""
    await saver.conn.execute("SELECT 1")


@pytest.fixture
def state(tmp_path: pathlib.Path) -> Iterator[ApplicationState]:
    """An application state over files of its own. A store a test left open is closed after
    the test by the store's own close."""
    built = build_application_state(fixture_settings(**files_in(tmp_path)), InMemorySaver())
    closes = [getattr(built, name).close for name in STORES]
    yield built
    for close in closes:
        close()


def test_a_stop_closes_every_store(tmp_path: pathlib.Path) -> None:
    app = create_app(fixture_settings(**files_in(tmp_path)))

    with TestClient(app, headers=SAME_ORIGIN) as client:
        state = state_of(client)
        assert closed(state) == set()

    assert closed(state) == set(STORES)


def test_the_stores_are_closed_in_one_order(
    state: ApplicationState, monkeypatch: pytest.MonkeyPatch
) -> None:
    seen: list[str] = []
    for name in STORES:
        store = getattr(state, name)
        monkeypatch.setattr(store, "close", remembering(seen, name, store.close))

    state.close()

    assert seen == list(STORES)
    assert closed(state) == set(STORES)


@pytest.mark.parametrize(
    ("failing", "kind"),
    [
        pytest.param("project_state", sqlite3.OperationalError, id="the first"),
        pytest.param("drafts", StoreBusy, id="the second, busy"),
        pytest.param("help_requests", sqlite3.OperationalError, id="one in the middle"),
        pytest.param("import_drafts", StoreBusy, id="the last, busy"),
    ],
)
def test_a_close_that_fails_leaves_every_other_store_closed(
    failing: str,
    kind: type[Exception],
    state: ApplicationState,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Wherever the failure is, the stores after it are closed as the ones before it are, the
    error raised is the close's own, and one line names the store."""
    error = kind("synthetic")
    fail_to_close(monkeypatch, state, failing, error)

    with caplog.at_level(logging.ERROR), pytest.raises(kind) as raised:
        state.close()

    assert raised.value is error
    assert closed(state) == set(STORES) - {failing}
    assert failures_logged(caplog) == [(could_not_close(failing), error)]


def test_two_closes_that_fail_are_both_logged_and_the_first_is_raised(
    state: ApplicationState,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    first = StoreBusy("synthetic")
    second = sqlite3.OperationalError("synthetic")
    fail_to_close(monkeypatch, state, "drafts", first)
    fail_to_close(monkeypatch, state, "help_requests", second)

    with caplog.at_level(logging.ERROR), pytest.raises(StoreBusy) as raised:
        state.close()

    assert raised.value is first
    assert closed(state) == set(STORES) - {"drafts", "help_requests"}
    assert failures_logged(caplog) == [
        (could_not_close("drafts"), first),
        (could_not_close("help_requests"), second),
    ]


def test_a_stop_whose_close_fails_still_fails_and_still_lets_go_of_the_household(
    tmp_path: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """The stop raises the close's own error. The other stores are closed, the saved-state
    store's connection is closed, and the claim on the household's files is released."""
    settings = fixture_settings(**files_in(tmp_path))
    client = TestClient(create_app(settings), headers=SAME_ORIGIN)
    error = StoreBusy("synthetic")

    client.__enter__()
    state = state_of(client)
    close_drafts = state.drafts.close
    with pytest.raises(AnotherProcessHasTheHousehold):
        claim_household(settings.database_path, settings.checkpoint_path)
    fail_to_close(monkeypatch, state, "drafts", error)
    try:
        with caplog.at_level(logging.ERROR), pytest.raises(StoreBusy) as raised:
            client.__exit__(None, None, None)

        assert raised.value is error
        assert closed(state) == set(STORES) - {"drafts"}
        assert failures_logged(caplog) == [(could_not_close("drafts"), error)]
        saver = state.checkpointer
        assert isinstance(saver, AsyncSqliteSaver)
        with pytest.raises(ValueError, match="Connection closed"):
            asyncio.run(read_saved_state(saver))
        claim_household(settings.database_path, settings.checkpoint_path).release()
    finally:
        close_drafts()


def test_an_interrupt_raised_inside_a_close_is_raised_at_once(
    state: ApplicationState,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """An interrupt is the process being told to stop, not a close that failed: it is not held
    back, nothing is logged for it, and the stores after it are left as they are."""
    fail_to_close(monkeypatch, state, "traces", KeyboardInterrupt())

    with caplog.at_level(logging.ERROR), pytest.raises(KeyboardInterrupt):
        state.close()

    assert closed(state) == {"project_state", "drafts"}
    assert failures_logged(caplog) == []
