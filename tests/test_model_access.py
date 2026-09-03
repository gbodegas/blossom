"""Two properties of model access that hold before any model is called.

The API key is read from the environment and kept out of every representation
of the settings, and hosted tracing for the model framework is off no matter
what the shell environment says.
"""

import os

import pytest
from fastapi.testclient import TestClient
from langsmith import utils as langsmith_utils

from blossom.app import create_app
from blossom.settings import (
    ANTHROPIC_API_KEY_VARIABLE,
    HOSTED_TRACING_VARIABLES,
    Settings,
    enforce_local_only_tracing,
)


def test_api_key_is_optional() -> None:
    assert Settings.from_environment({}).anthropic_api_key is None
    assert Settings.from_environment({ANTHROPIC_API_KEY_VARIABLE: "   "}).anthropic_api_key is None


def test_api_key_is_read_and_stripped() -> None:
    settings = Settings.from_environment({ANTHROPIC_API_KEY_VARIABLE: "  sk-test-value  "})

    assert settings.anthropic_api_key == "sk-test-value"


def test_api_key_never_appears_in_repr_or_str() -> None:
    settings = Settings.from_environment({ANTHROPIC_API_KEY_VARIABLE: "sk-test-value"})

    assert "sk-test-value" not in repr(settings)
    assert "sk-test-value" not in str(settings)


def test_enforce_local_only_tracing_overrides_an_enabled_environment() -> None:
    environ = {variable: "true" for variable in HOSTED_TRACING_VARIABLES}

    enforce_local_only_tracing(environ)

    assert all(environ[variable] == "false" for variable in HOSTED_TRACING_VARIABLES)


def test_app_startup_forces_hosted_tracing_off(monkeypatch: pytest.MonkeyPatch) -> None:
    """A stray LANGSMITH_TRACING=true in a shell must not turn on remote tracing."""
    for variable in HOSTED_TRACING_VARIABLES:
        monkeypatch.setenv(variable, "true")
    assert langsmith_utils.tracing_is_enabled() is True

    with TestClient(create_app()):
        pass

    assert all(os.environ[variable] == "false" for variable in HOSTED_TRACING_VARIABLES)
    assert langsmith_utils.tracing_is_enabled() is False
