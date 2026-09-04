"""Saved graph state: on a safe path, strict about what it revives, versioned.

A pause at the approval gate and the decision that ends it survive the process
that wrote them, come back as the graph's own types, and carry the version of
the graph that wrote them so a changed graph does not resume them blindly.
"""

import asyncio
import os
import pathlib
import sqlite3
from typing import Any, cast

import pytest
from fastapi.testclient import TestClient
from langgraph.checkpoint.memory import InMemorySaver
from langgraph.checkpoint.sqlite.aio import AsyncSqliteSaver
from langgraph.types import Command, StateSnapshot
from pydantic import BaseModel

from blossom.agent.gates import ApprovalState, build_approval_graph
from blossom.agent.runs import (
    DURABILITY,
    GRAPH_VERSION,
    GRAPH_VERSION_KEY,
    RECURSION_LIMIT,
    StaleGraphVersion,
    ensure_current_version,
    recorded_version,
    run_config,
)
from blossom.app import create_app
from blossom.dependencies import STATE_ATTRIBUTE, ApplicationState
from blossom.drafts import Draft, DraftStatus
from blossom.settings import CHECKPOINT_PATH_VARIABLE
from blossom.stores import checkpoints
from blossom.stores.checkpoints import (
    DRIVE_REMOTE,
    STATE_TYPES,
    UnsafeCheckpointPath,
    checkpoint_serializer,
    drive_is_network,
    local_form,
    open_checkpointer,
    refuse_unsafe_path,
)
from tests.support import fixture_settings

# ------------------------------------------------------------------- the path


@pytest.mark.parametrize(
    "text",
    [
        r"\\server\share\checkpoints.sqlite3",
        "//server/share/checkpoints.sqlite3",
        r"\\?\UNC\server\share\checkpoints.sqlite3",
        ":memory:",
    ],
)
def test_network_shares_and_memory_databases_are_refused(text: str) -> None:
    """Read from the configured text, so a share is refused on any platform,
    not only on the one whose slashes it is written in."""
    with pytest.raises(UnsafeCheckpointPath):
        refuse_unsafe_path(pathlib.Path(text), environ={})


@pytest.mark.parametrize(
    "folder", ["OneDrive", "OneDrive - Contoso", "Dropbox", "Google Drive", "iCloudDrive"]
)
def test_a_synced_folder_anywhere_in_the_path_is_refused(
    folder: str, tmp_path: pathlib.Path
) -> None:
    with pytest.raises(UnsafeCheckpointPath, match="synced"):
        refuse_unsafe_path(tmp_path / folder / "deep" / "checkpoints.sqlite3", environ={})


def test_the_sync_clients_own_root_is_refused_by_environment(tmp_path: pathlib.Path) -> None:
    root = tmp_path / "synced-root"
    inside = root / "checkpoints.sqlite3"
    outside = tmp_path / "elsewhere" / "checkpoints.sqlite3"

    with pytest.raises(UnsafeCheckpointPath, match="OneDrive"):
        refuse_unsafe_path(inside, environ={"OneDrive": str(root)})
    assert refuse_unsafe_path(outside, environ={"OneDrive": str(root)}) == outside


def test_a_local_file_path_is_accepted(tmp_path: pathlib.Path) -> None:
    path = tmp_path / "checkpoints.sqlite3"

    assert refuse_unsafe_path(path, environ={}) == path


@pytest.mark.parametrize("folder", ["MyOneDriveBackups", "dropboxes", "not-icloud-drive"])
def test_a_folder_that_merely_contains_a_sync_name_is_accepted(
    folder: str, tmp_path: pathlib.Path
) -> None:
    """A whole component, or one with a separator after the name, is a synced
    folder. A name that merely contains one is somebody's ordinary directory."""
    path = tmp_path / folder / "checkpoints.sqlite3"

    assert refuse_unsafe_path(path, environ={}) == path


def test_a_path_that_walks_into_a_synced_folder_is_refused(tmp_path: pathlib.Path) -> None:
    """The guard reads where the path lands, not how it was spelled."""
    walked = tmp_path / "plain" / ".." / "OneDrive" / "checkpoints.sqlite3"

    with pytest.raises(UnsafeCheckpointPath, match="synced"):
        refuse_unsafe_path(walked, environ={})


def test_a_path_that_walks_out_of_a_synced_folder_is_accepted(tmp_path: pathlib.Path) -> None:
    walked = tmp_path / "OneDrive" / ".." / "plain" / "checkpoints.sqlite3"

    assert refuse_unsafe_path(walked, environ={}) == walked


def test_the_icloud_location_on_macos_is_refused() -> None:
    icloud = pathlib.Path("/Users/someone/Library/Mobile Documents/com~apple~CloudDocs/cp.sqlite3")

    with pytest.raises(UnsafeCheckpointPath, match="synced"):
        refuse_unsafe_path(icloud, environ={})


def test_a_drive_letter_mapped_to_a_share_is_refused(
    monkeypatch: pytest.MonkeyPatch, tmp_path: pathlib.Path
) -> None:
    """A mapped drive is a network share wearing a local name."""
    monkeypatch.setattr(checkpoints, "drive_is_network", lambda text: True)

    with pytest.raises(UnsafeCheckpointPath, match="network share"):
        refuse_unsafe_path(tmp_path / "checkpoints.sqlite3", environ={})


@pytest.mark.skipif(os.name != "nt", reason="drive letters are a Windows idea")
def test_a_local_drive_letter_is_not_read_as_a_network_one(tmp_path: pathlib.Path) -> None:
    """The real call, not the stand-in: a fixed drive must answer no."""
    assert DRIVE_REMOTE == 4
    assert drive_is_network(str(tmp_path)) is False


@pytest.mark.skipif(os.name != "nt", reason="the extended-length prefix is a Windows spelling")
def test_an_extended_length_local_path_is_not_mistaken_for_a_share(tmp_path: pathlib.Path) -> None:
    extended = pathlib.Path("\\\\?\\" + str(tmp_path / "checkpoints.sqlite3"))

    assert refuse_unsafe_path(extended, environ={}) == extended
    assert local_form(extended) == str(tmp_path / "checkpoints.sqlite3")


# -------------------------------------------------------------- the serializer


class LooksLikeADraft(BaseModel):
    """A class the graph does not carry."""

    body: str


def test_the_serializer_revives_the_graphs_own_types() -> None:
    serde = checkpoint_serializer()
    draft = Draft(body="Until Friday?", status=DraftStatus.APPROVED_FOR_MANUAL_SEND)

    revived = serde.loads_typed(serde.dumps_typed(draft))

    assert isinstance(revived, Draft)
    assert revived == draft
    assert revived.status is DraftStatus.APPROVED_FOR_MANUAL_SEND


def test_the_serializer_does_not_revive_a_class_outside_the_allowlist() -> None:
    """Strict mode is the point: an unlisted class comes back as data, not an object."""
    serde = checkpoint_serializer()

    revived = serde.loads_typed(serde.dumps_typed(LooksLikeADraft(body="x")))

    assert not isinstance(revived, LooksLikeADraft)


def test_every_type_a_gate_carries_is_on_the_allowlist() -> None:
    assert Draft in STATE_TYPES
    assert DraftStatus in STATE_TYPES


# ------------------------------------------------------------------ the store


def test_open_checkpointer_creates_the_file_and_overwrites_deleted_rows(
    tmp_path: pathlib.Path,
) -> None:
    path = tmp_path / "nested" / "checkpoints.sqlite3"

    async def scenario() -> int:
        async with open_checkpointer(path) as saver:
            assert isinstance(saver, AsyncSqliteSaver)
            async with saver.conn.execute("PRAGMA secure_delete") as cursor:
                row = await cursor.fetchone()
        assert row is not None
        return int(row[0])

    assert asyncio.run(scenario()) == 1
    assert path.is_file()


def test_a_pause_and_its_decision_survive_the_process_that_wrote_them(
    tmp_path: pathlib.Path,
) -> None:
    """Two separate event loops stand in for two processes: one pauses, the other resumes."""
    path = tmp_path / "checkpoints.sqlite3"
    draft = Draft(body="Requesting an extension.")
    config = run_config("plan:student:2026-08-19")

    async def first_process() -> None:
        async with open_checkpointer(path) as saver:
            graph = build_approval_graph(saver)
            paused = await graph.ainvoke(
                ApprovalState(draft=draft), config=config, durability=DURABILITY
            )
            assert len(paused["__interrupt__"]) == 1

    async def second_process() -> StateSnapshot:
        async with open_checkpointer(path) as saver:
            graph = build_approval_graph(saver)
            waiting = await graph.aget_state(config)
            assert waiting.next == ("require_human_approval",)
            assert isinstance(waiting.values["draft"], Draft)
            ensure_current_version(waiting)
            resume: Command[Any] = Command(resume={"approved": True, "reason": "ok"})
            await graph.ainvoke(resume, config=config, durability=DURABILITY)
            return await graph.aget_state(config)

    asyncio.run(first_process())
    final = asyncio.run(second_process())

    assert final.next == ()
    assert isinstance(final.values["draft"], Draft)
    assert final.values["draft"].status is DraftStatus.APPROVED_FOR_MANUAL_SEND
    assert final.values["decision"] == "approved"
    assert recorded_version(final) == GRAPH_VERSION
    with sqlite3.connect(path) as connection:
        assert connection.execute("PRAGMA journal_mode").fetchone()[0] == "wal"


# ------------------------------------------------------------------ the version


def test_run_config_carries_the_thread_the_version_and_the_limit_and_nothing_else() -> None:
    config = run_config("plan:student:2026-08-19")

    assert config["configurable"] == {"thread_id": "plan:student:2026-08-19"}
    assert config["metadata"] == {GRAPH_VERSION_KEY: GRAPH_VERSION}
    assert config["recursion_limit"] == RECURSION_LIMIT
    assert set(config) == {"configurable", "metadata", "recursion_limit"}


def test_the_run_contract_holds_the_values_it_promises() -> None:
    """Asserting the constants against themselves would pass for any value, and
    both of these exist to be far from what the framework would otherwise do."""
    assert DURABILITY == "sync"
    assert RECURSION_LIMIT < 100


def test_a_thread_written_by_another_graph_version_is_refused() -> None:
    graph = build_approval_graph(InMemorySaver())
    config = run_config("plan:student:2026-08-20")
    graph.invoke(ApprovalState(draft=Draft(body="x")), config=config, durability=DURABILITY)
    current = graph.get_state(config)
    ensure_current_version(current)

    older = current._replace(metadata=cast(Any, {GRAPH_VERSION_KEY: GRAPH_VERSION - 1}))
    unstamped = current._replace(metadata=cast(Any, {}))

    with pytest.raises(StaleGraphVersion, match="version"):
        ensure_current_version(older)
    with pytest.raises(StaleGraphVersion, match="None"):
        ensure_current_version(unstamped)


@pytest.mark.parametrize("written", [True, 1.0, "1", None, [1]])
def test_only_a_whole_number_reads_as_a_version(written: object) -> None:
    """Metadata is plain JSON, outside the strict serializer, so anything can be
    in the field. ``True`` matters on its own: Python counts a bool as an int."""
    graph = build_approval_graph(InMemorySaver())
    config = run_config("plan:student:2026-08-21")
    graph.invoke(ApprovalState(draft=Draft(body="x")), config=config, durability=DURABILITY)
    snapshot = graph.get_state(config)._replace(metadata=cast(Any, {GRAPH_VERSION_KEY: written}))

    assert recorded_version(snapshot) is None
    with pytest.raises(StaleGraphVersion):
        ensure_current_version(snapshot)


# ----------------------------------------------------------------- the lifespan


def test_the_lifespan_opens_the_store_on_the_configured_path(tmp_path: pathlib.Path) -> None:
    path = tmp_path / "checkpoints.sqlite3"
    settings = fixture_settings(**{CHECKPOINT_PATH_VARIABLE: str(path)})
    app = create_app(settings)

    with TestClient(app) as client:
        assert client.get("/student/due-this-week").status_code == 200
        state: ApplicationState = getattr(app.state, STATE_ATTRIBUTE)
        assert isinstance(state.checkpointer, AsyncSqliteSaver)

    assert path.is_file()


def test_startup_refuses_a_checkpoint_path_inside_a_synced_folder(tmp_path: pathlib.Path) -> None:
    settings = fixture_settings(
        **{CHECKPOINT_PATH_VARIABLE: str(tmp_path / "OneDrive" / "checkpoints.sqlite3")}
    )

    with pytest.raises(UnsafeCheckpointPath), TestClient(create_app(settings)):
        pass
