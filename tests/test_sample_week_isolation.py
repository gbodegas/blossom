# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Gerardo Bodegas Martinez
"""A session labeled "Sample week" keeps all of its state in the sample's own folder.

The sample's launch file puts the three state files under ``.local/sample/``, but a
``BLOSSOM_`` path already set in the terminal takes precedence over a launch file. With the
flag on, a start whose files would land anywhere else is refused, naming the folder, before
anything is claimed, created or opened, so a sample can neither show the household's record
nor change it. The flag alone puts the defaults in that folder too. The household's own launch
is unchanged.
"""

import hashlib
import os
import sqlite3
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from blossom import settings as settings_module
from blossom.settings import (
    CHECKPOINT_PATH_VARIABLE,
    DATABASE_PATH_VARIABLE,
    TRACE_PATH_VARIABLE,
    Settings,
)
from tests.support import (
    HERS,
    PAGE_HEADERS,
    THEIRS,
    client_for,
    fixture_settings,
    form_fields,
    linked_to,
    store_of,
)

NAMES = ("blossom.sqlite3", "checkpoints.sqlite3", "traces.sqlite3")
LOCKS = ("blossom.lock", "checkpoints.lock")
SIDECARS = ("-journal", "-wal", "-shm")
SIGNED_IN = {"BLOSSOM_STUDENT_PASSPHRASE": HERS, "BLOSSOM_PARENT_PASSPHRASE": THEIRS}
"""The sign-in on, so a start reads or makes the secret beside the database too."""
VARIABLES = (DATABASE_PATH_VARIABLE, CHECKPOINT_PATH_VARIABLE, TRACE_PATH_VARIABLE)
HOUSEHOLD_NOTE = "Made-up household note: return the library book"
SAMPLE_NOTE = "Made-up sample note: bring a shoebox"
OVERRIDES = {
    "database": (DATABASE_PATH_VARIABLE,),
    "checkpoints": (CHECKPOINT_PATH_VARIABLE,),
    "traces": (TRACE_PATH_VARIABLE,),
    "all three": VARIABLES,
}
"""Which of the household's paths the terminal supplies, each on its own and all three."""


def sample_folder() -> Path:
    """The sample's own folder: ``.local/sample/`` in a checkout, a temporary one here."""
    return settings_module.LOCAL_STATE_PATH / "sample"


def the_three_in(folder: Path) -> dict[str, str]:
    return {variable: str(folder / name) for variable, name in zip(VARIABLES, NAMES, strict=True)}


def sample_launch(**terminal: str) -> Settings:
    """What a sample launch reads: the flag, the pinned day and the sample's own paths, with
    whatever the terminal already set taking precedence, as it does over a launch file."""
    launch = {"BLOSSOM_SAMPLE": "1", "BLOSSOM_TODAY": "2026-09-07", **the_three_in(sample_folder())}
    return fixture_settings(**{**launch, **terminal})


def save_a_note(client: TestClient, text: str) -> None:
    """Write down homework from her page, the way the sample tour's third step does."""
    page = client.get("/student/homework-notes/new", headers=PAGE_HEADERS).text
    fields = form_fields(page, "/student/actions/homework-notes")
    saved = client.post(
        "/student/actions/homework-notes",
        data={**fields, "text": text, "course": "", "due_date": ""},
    )
    assert saved.status_code == 303, saved.text[:300]


def notes_of(client: TestClient) -> list[str]:
    return [note.text for note in store_of(client).outstanding_captures().notes]


def a_household(folder: Path) -> Path:
    """The family's own state in ``folder``: a household start with a made-up note saved.

    The two lock files its start leaves are removed, so a later start that claims the
    household's files has to make them again, and ``snapshot`` shows it.
    """
    with client_for(fixture_settings(**the_three_in(folder))) as client:
        save_a_note(client, HOUSEHOLD_NOTE)
        assert notes_of(client) == [HOUSEHOLD_NOTE]
    for name in LOCKS:
        (folder / name).unlink()
    return folder


def snapshot(folder: Path) -> dict[str, tuple[str, int, int]]:
    """Every entry in ``folder`` by name: a link by where it points, a file by its contents'
    digest, each with its size and modification time."""
    found = {}
    for path in sorted(folder.iterdir()):
        status = path.lstat()
        if path.is_symlink():
            content = f"a link to {os.readlink(path)}"
        else:
            content = hashlib.sha256(path.read_bytes()).hexdigest()
        found[path.name] = (content, status.st_size, status.st_mtime_ns)
    return found


def link(target: Path, at: Path) -> None:
    """Put a symbolic link to ``target`` at ``at``, or skip where none can be made."""
    try:
        at.symlink_to(target)
    except OSError:
        pytest.skip("no symbolic link can be made here")


@contextmanager
def connections_recorded(monkeypatch: pytest.MonkeyPatch) -> Iterator[list[Path]]:
    """Every file SQLite is asked to open, where it really lands: each store and the
    checkpointer open their file through ``sqlite3.connect``."""
    opened: list[Path] = []
    connect: Callable[..., sqlite3.Connection] = sqlite3.connect

    def recording(
        database: str | os.PathLike[str], *args: object, **kwargs: object
    ) -> sqlite3.Connection:
        opened.append(Path(os.path.realpath(database)))
        return connect(database, *args, **kwargs)

    with monkeypatch.context() as patch:
        patch.setattr(sqlite3, "connect", recording)
        yield opened


def outside(what: str, correction: str) -> str:
    """The whole refusal for a sample file or folder outside the sample's folder, word for
    word: what lands where, and what to correct in that case."""
    return (
        f"BLOSSOM_SAMPLE is on, but {what}: a sample week keeps all its state there, so it "
        f"never reads or changes other records. {correction}, then start the sample again"
    )


def a_sample_session(settings: Settings) -> tuple[ValueError | None, list[str]]:
    """Start the sample, read her notes and save a made-up one. The refusal, when the start
    is refused, and the notes the session read."""
    try:
        with client_for(settings) as client:
            read = notes_of(client)
            save_a_note(client, SAMPLE_NOTE)
    except ValueError as refusal:
        return refusal, []
    return None, read


@pytest.mark.parametrize("supplied", OVERRIDES.values(), ids=OVERRIDES.keys())
def test_a_household_path_from_the_terminal_is_refused_before_anything_is_opened(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, supplied: tuple[str, ...]
) -> None:
    household = a_household(tmp_path / "household")
    before = snapshot(household)
    terminal = {variable: the_three_in(household)[variable] for variable in supplied}

    with connections_recorded(monkeypatch) as opened:
        refused, read = a_sample_session(sample_launch(**terminal))

    assert HOUSEHOLD_NOTE not in read, "the sample session read the household's record"
    assert [path for path in opened if path.is_relative_to(household.resolve())] == []
    assert snapshot(household) == before, "the sample session changed the household's files"
    assert refused is not None, "the sample session started on the household's files"
    variable = supplied[0]
    assert str(refused) == outside(
        f"{variable} names {terminal[variable]}, outside {sample_folder()}",
        f"Unset {variable} in the terminal",
    )


@pytest.mark.parametrize("reach", ["a link", "a walk through ..", "a linked folder"])
def test_a_sample_file_that_lands_in_the_household_is_refused(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, reach: str
) -> None:
    """The folder a file really lands in counts, not the way its path is spelled."""
    household = a_household(tmp_path / "household")
    before = snapshot(household)
    sample_folder().mkdir(parents=True)
    lands = household.resolve()
    if reach == "a link":
        database = sample_folder() / NAMES[0]
        link(household / NAMES[0], database)
        expected = outside(
            f"{database} leads into {lands}, outside {sample_folder()}",
            f"Remove the link at {database}",
        )
    elif reach == "a walk through ..":
        database = sample_folder() / ".." / household.name / NAMES[0]
        expected = outside(
            f"{DATABASE_PATH_VARIABLE} names {database}, outside {sample_folder()}",
            f"Unset {DATABASE_PATH_VARIABLE} in the terminal",
        )
    else:
        inner = sample_folder() / "inner"
        if linked_to(household, inner) is None:
            pytest.skip("no link to a folder can be made here")
        database = inner / NAMES[0]
        expected = outside(
            f"{inner} leads into {lands}, outside {sample_folder()}",
            f"Remove the link in {sample_folder()} that leads there",
        )

    with connections_recorded(monkeypatch) as opened:
        refused, read = a_sample_session(sample_launch(**{DATABASE_PATH_VARIABLE: str(database)}))

    assert HOUSEHOLD_NOTE not in read
    assert [path for path in opened if path.is_relative_to(household.resolve())] == []
    assert snapshot(household) == before
    assert refused is not None
    assert str(refused) == expected


@pytest.mark.parametrize("name", ["checkpoints.sqlite3-wal", "blossom.lock", "household.secret"])
def test_a_file_beside_the_sample_databases_that_leads_to_the_household_is_refused(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, name: str
) -> None:
    """Every file a start opens or makes beside the databases counts: a write-ahead log, a
    lock, the sign-in secret. Each is checked where it really lands."""
    household = a_household(tmp_path / "household")
    before = snapshot(household)
    sample_folder().mkdir(parents=True)
    link(household / name, sample_folder() / name)

    with connections_recorded(monkeypatch) as opened:
        refused, read = a_sample_session(sample_launch())

    assert HOUSEHOLD_NOTE not in read
    assert [path for path in opened if path.is_relative_to(household.resolve())] == []
    assert snapshot(household) == before
    assert refused is not None
    assert str(refused) == outside(
        f"{sample_folder() / name} leads into {household.resolve()}, outside {sample_folder()}",
        f"Remove the link at {sample_folder() / name}",
    )


def test_a_sample_folder_that_is_a_link_is_refused(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A link at the sample folder itself would make every file land "inside" it, wherever
    the link leads, so the folder must be a folder of its own."""
    household = a_household(tmp_path / "household")
    before = snapshot(household)
    if linked_to(household, sample_folder()) is None:
        pytest.skip("no link to a folder can be made here")

    with connections_recorded(monkeypatch) as opened:
        refused, read = a_sample_session(sample_launch())

    assert HOUSEHOLD_NOTE not in read, "the sample session read the household's record"
    assert [path for path in opened if path.is_relative_to(household.resolve())] == []
    assert snapshot(household) == before, "the sample session changed the household's files"
    assert refused is not None, "the sample session started on the household's files"
    assert str(refused) == (
        f"BLOSSOM_SAMPLE is on, but {sample_folder()} is a link to {household.resolve()}: a "
        "sample week keeps its state in a folder of its own, never one that is itself a link. "
        f"Remove the link at {sample_folder()}, then start the sample again"
    )


def test_a_database_named_in_another_folder_is_refused_even_when_its_files_lead_back(
    tmp_path: Path,
) -> None:
    """The sign-in secret and the part it is written through are made beside the database's
    configured name, so that folder must be the sample's own too, even when every state file
    named there is a link into the sample folder."""
    sample_folder().mkdir(parents=True)
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    database = NAMES[0]
    for name in (database, *(database + suffix for suffix in SIDECARS), "household.secret"):
        link(sample_folder() / name, elsewhere / name)
    before = snapshot(elsewhere)
    settings = sample_launch(**{DATABASE_PATH_VARIABLE: str(elsewhere / database)}, **SIGNED_IN)

    try:
        with client_for(settings):
            refused = None
    except ValueError as refusal:
        refused = refusal

    assert snapshot(elsewhere) == before, "the start made files beside the configured database"
    assert refused is not None
    assert str(refused) == outside(
        f"{DATABASE_PATH_VARIABLE} names {elsewhere / database}, outside {sample_folder()}",
        f"Unset {DATABASE_PATH_VARIABLE} in the terminal",
    )


def test_a_sample_edit_stays_in_the_sample_folder_across_a_restart(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    with connections_recorded(monkeypatch) as opened:
        refused, _ = a_sample_session(sample_launch())
        with client_for(sample_launch()) as client:
            after_restart = notes_of(client)

    assert refused is None
    assert after_restart == [SAMPLE_NOTE]
    assert opened
    assert [path for path in opened if not path.is_relative_to(sample_folder().resolve())] == []


def test_the_flag_alone_keeps_the_sample_in_its_folder_and_the_household_defaults_stay() -> None:
    local = settings_module.LOCAL_STATE_PATH
    sample = fixture_settings(BLOSSOM_SAMPLE="1")
    household = fixture_settings()

    assert (sample.database_path, sample.checkpoint_path, sample.trace_path) == tuple(
        local / "sample" / name for name in NAMES
    )
    assert (household.database_path, household.checkpoint_path, household.trace_path) == tuple(
        local / name for name in NAMES
    )


def test_the_household_launch_still_opens_the_paths_it_is_given(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    folder = tmp_path / "household"

    with connections_recorded(monkeypatch) as opened:
        a_household(folder)

    assert {path.name for path in opened} >= set(NAMES)
    assert [path for path in opened if not path.is_relative_to(folder.resolve())] == []
