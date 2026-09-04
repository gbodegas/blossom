"""Tests for settings resolution.

Every configured path resolves to an absolute location under the repository,
so the application starts from any working directory. The last test starts the
app from an unrelated directory and asserts a page renders.
"""

import pathlib

import pytest
from fastapi.testclient import TestClient

from blossom.app import create_app
from blossom.settings import (
    CHECKPOINT_PATH_VARIABLE,
    CHROMA_PATH_VARIABLE,
    DATABASE_PATH_VARIABLE,
    FIXTURE_PATH_VARIABLE,
    REPOSITORY_ROOT,
    Settings,
    resolve_configured_path,
)


def test_defaults_are_absolute_and_point_at_real_package_assets() -> None:
    settings = Settings.from_environment({})

    assert settings.fixture_path.is_absolute()
    assert settings.database_path.is_absolute()
    assert settings.checkpoint_path.is_absolute()
    assert settings.checkpoint_path != settings.database_path
    assert settings.chroma_path.is_absolute()
    assert (settings.fixture_path / "assignments.json").is_file()
    assert (settings.static_path / "blossom.css").is_file()
    assert (settings.template_path / "student_due_this_week.html").is_file()


def test_environment_variables_override_every_configurable_path(tmp_path: pathlib.Path) -> None:
    """Uses ``tmp_path`` because ``/tmp/x`` has no drive letter and is not absolute on Windows."""
    fixtures = tmp_path / "fixtures"
    database = tmp_path / "state.sqlite3"
    checkpoints = tmp_path / "checkpoints.sqlite3"
    chroma = tmp_path / "chroma"

    settings = Settings.from_environment(
        {
            FIXTURE_PATH_VARIABLE: str(fixtures),
            DATABASE_PATH_VARIABLE: str(database),
            CHECKPOINT_PATH_VARIABLE: str(checkpoints),
            CHROMA_PATH_VARIABLE: str(chroma),
        }
    )

    assert settings.fixture_path == fixtures
    assert settings.database_path == database
    assert settings.checkpoint_path == checkpoints
    assert settings.chroma_path == chroma


def test_blank_environment_values_fall_back_to_defaults() -> None:
    """An exported but empty variable is usually a shell accident, not a request for ''."""
    settings = Settings.from_environment({FIXTURE_PATH_VARIABLE: "   "})

    assert settings.fixture_path == Settings.from_environment({}).fixture_path


def test_relative_values_resolve_against_the_repository_not_the_working_directory() -> None:
    """Repository-relative resolution gives the paths in ``.env.example`` one fixed meaning."""
    resolved = resolve_configured_path(".local/blossom.sqlite3")

    assert resolved == (REPOSITORY_ROOT / ".local" / "blossom.sqlite3").resolve()


def test_application_serves_a_page_when_started_from_another_directory(
    monkeypatch: pytest.MonkeyPatch, tmp_path: pathlib.Path
) -> None:
    """The app starts from a working directory outside the repository."""
    monkeypatch.chdir(tmp_path)

    with TestClient(create_app()) as client:
        response = client.get("/student/due-this-week")

    assert response.status_code == 200
    assert "Due this week" in response.text
