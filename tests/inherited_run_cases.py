"""What a test sees in a run whose shell named a household's files.

Not collected with the suite: ``tests/test_state_guard.py`` runs this file in a
run of its own, with the three runtime-path variables set, and reads the
result. Each case would fail in an ordinary run, where nothing is inherited.
"""

import os
import pathlib

import pytest
from fastapi.testclient import TestClient

from blossom import app as app_module
from blossom.app import create_app
from blossom.settings import get_settings
from tests.state_guard import RUNTIME_PATH_VARIABLES, HouseholdStateProtected, StateGuard


def test_the_run_noted_what_it_inherited_and_removed_it(state_guard: StateGuard) -> None:
    assert sorted(state_guard.inherited) == sorted(RUNTIME_PATH_VARIABLES)
    assert [variable for variable in RUNTIME_PATH_VARIABLES if variable in os.environ] == []


def test_an_ordinary_start_lands_in_temporary_state(tmp_path: pathlib.Path) -> None:
    """Settings were cached from the inherited variables when the application was imported."""
    assert get_settings().database_path == tmp_path / "blossom.sqlite3"

    with TestClient(create_app()):
        pass

    assert (tmp_path / "blossom.sqlite3").exists()


def test_the_inherited_folder_is_refused(state_guard: StateGuard) -> None:
    for folder in state_guard.inherited.values():
        with pytest.raises(HouseholdStateProtected, match="pointed when the run began"):
            state_guard.refuse(folder / "blossom.sqlite3")


def test_the_application_built_at_import_names_the_inherited_file_and_is_refused(
    state_guard: StateGuard,
) -> None:
    """Built before the variables were removed, so it is the one thing still pointing there."""
    folder = state_guard.inherited[RUNTIME_PATH_VARIABLES[0]]

    refused = pytest.raises(HouseholdStateProtected, match="pointed when the run began")
    with refused, TestClient(app_module.app):
        pass

    assert not folder.exists()
