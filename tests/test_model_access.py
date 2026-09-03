"""Two properties of model access that hold before any model is called.

The API key is read from the environment and kept out of ``repr`` and ``str``
of the settings, and hosted tracing for the model framework is off after
startup no matter which of its environment spellings a shell set.
"""

import os
from collections.abc import Iterator

import pytest
from fastapi.testclient import TestClient
from langchain_core.callbacks.manager import CallbackManager
from langchain_core.tracers.langchain import LangChainTracer
from langsmith import utils as langsmith_utils

from blossom.app import create_app
from blossom.settings import (
    ANTHROPIC_API_KEY_VARIABLE,
    HOSTED_TRACING_VARIABLES,
    Settings,
    enforce_local_only_tracing,
)


def clear_tracing_cache() -> None:
    """langsmith caches environment reads; drop them so the next read is real."""
    cache_clear = getattr(langsmith_utils.get_env_var, "cache_clear", None)
    assert cache_clear is not None, "langsmith does not cache get_env_var here; revisit settings"
    cache_clear()


@pytest.fixture
def fresh_tracing_cache() -> Iterator[None]:
    """Clear langsmith's cached environment reads before and after each test."""
    clear_tracing_cache()
    yield
    clear_tracing_cache()


def hosted_tracer_attached() -> bool:
    """Ask the framework's real consumer whether it would attach a hosted tracer."""
    handlers = CallbackManager.configure().handlers
    return any(isinstance(handler, LangChainTracer) for handler in handlers)


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


@pytest.mark.parametrize("variable", HOSTED_TRACING_VARIABLES)
def test_app_startup_forces_hosted_tracing_off(
    variable: str, monkeypatch: pytest.MonkeyPatch, fresh_tracing_cache: None
) -> None:
    """Each spelling the framework honors is enough to enable tracing on its own,
    and startup must turn every one of them off."""
    for other in HOSTED_TRACING_VARIABLES:
        monkeypatch.delenv(other, raising=False)
    monkeypatch.setenv(variable, "true")
    clear_tracing_cache()
    assert langsmith_utils.tracing_is_enabled() is True

    with TestClient(create_app()):
        pass

    assert all(os.environ[name] == "false" for name in HOSTED_TRACING_VARIABLES)
    assert langsmith_utils.tracing_is_enabled() is False
    assert hosted_tracer_attached() is False


def test_legacy_tracing_variable_does_not_break_callback_setup(
    monkeypatch: pytest.MonkeyPatch, fresh_tracing_cache: None
) -> None:
    """LANGCHAIN_TRACING=true with v2 off makes the framework raise; startup must
    write it to false rather than leave it in place."""
    monkeypatch.setenv("LANGCHAIN_TRACING", "true")
    clear_tracing_cache()

    with TestClient(create_app()):
        pass

    assert CallbackManager.configure().handlers == []


def test_construction_leaves_the_environment_alone_until_startup(
    monkeypatch: pytest.MonkeyPatch, fresh_tracing_cache: None
) -> None:
    """Importing the module and calling create_app change nothing; the lifespan does.

    uvicorn needs the module-level app object, so anything create_app did to the
    process environment would run on a bare import by any embedder.
    """
    monkeypatch.setenv("LANGSMITH_TRACING", "true")

    app = create_app()

    assert os.environ["LANGSMITH_TRACING"] == "true"
    with TestClient(app):
        assert os.environ["LANGSMITH_TRACING"] == "false"
