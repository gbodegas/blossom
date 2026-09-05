"""Properties of model access that hold before any model is called.

The API key is read from the environment and kept out of ``repr`` and ``str``
of the settings; hosted tracing for the model framework is off after startup
no matter which of its environment spellings a shell set; and the one model
client the package can construct sends to the public endpoint through an HTTP
client that ignores the environment, carries no provider-side tools, and
refuses to exist without a key.
"""

import dataclasses
import os
from collections.abc import Iterator

import pytest
from fastapi.testclient import TestClient
from langchain_anthropic import ChatAnthropic
from langchain_anthropic._client_utils import _get_default_httpx_client
from langchain_core.callbacks.manager import CallbackManager
from langchain_core.tracers.context import tracing_v2_enabled
from langsmith import utils as langsmith_utils
from pydantic import SecretStr

from blossom.anthropic_client import (
    ENDPOINT,
    MAX_TOKENS,
    MODEL,
    TIMEOUT_SECONDS,
    ModelUnavailable,
    chat_model,
)
from blossom.app import create_app
from blossom.settings import (
    ANTHROPIC_API_KEY_VARIABLE,
    HOSTED_TRACING_VARIABLES,
    TRACE_PAYLOAD_VARIABLES,
    Settings,
    enforce_local_only_tracing,
)
from tests.support import hosted_tracer_attached

# Variables the model library or the HTTP client beneath it reads to pick an
# endpoint, a proxy, or a certificate store. Each alone changes a client built
# with the defaults; none may change the one this package builds.
REROUTING_VARIABLES: dict[str, str] = {
    "ANTHROPIC_API_URL": "https://elsewhere.example/v1",
    "ANTHROPIC_BASE_URL": "https://elsewhere.example/v1",
    "ANTHROPIC_PROXY": "http://proxy.example:8080",
    "LANGSMITH_GATEWAY": "true",
    "HTTP_PROXY": "http://proxy.example:8080",
    "HTTPS_PROXY": "http://proxy.example:8080",
    "ALL_PROXY": "http://proxy.example:8080",
    "SSL_CERT_FILE": "C:/nowhere/not-a-certificate.pem",
}
KEY_VARIABLES = ("LANGSMITH_GATEWAY_API_KEY", "LANGSMITH_API_KEY")


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


@pytest.fixture
def clean_routing_environment(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """No rerouting variable set, and the integration's cached default client dropped.

    The integration caches its default HTTP client by endpoint, timeout, and
    proxy, so a client built earlier in the process would otherwise decide what
    a later test sees.
    """
    for variable in (*REROUTING_VARIABLES, *KEY_VARIABLES):
        monkeypatch.delenv(variable, raising=False)
    _get_default_httpx_client.cache_clear()
    yield
    _get_default_httpx_client.cache_clear()


def settings_with_key() -> Settings:
    return Settings.from_environment({ANTHROPIC_API_KEY_VARIABLE: "sk-test-value"})


def assert_pinned(model: ChatAnthropic) -> None:
    """The client sends to the public endpoint through transports that ignore the shell."""
    assert model.anthropic_api_url == ENDPOINT
    assert model.anthropic_proxy is None
    assert model.anthropic_api_key.get_secret_value() == "sk-test-value"
    for http_client in (model._client._client, model._async_client._client):
        assert str(http_client.base_url).rstrip("/") in ("", ENDPOINT)
        assert http_client._trust_env is False
        assert http_client._mounts == {}
    assert str(model._client.base_url).rstrip("/") == ENDPOINT


# ------------------------------------------------------------------ the key


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


# -------------------------------------------------------------- the client


def test_a_model_needs_a_key_and_says_which() -> None:
    with pytest.raises(ModelUnavailable, match="ANTHROPIC_API_KEY"):
        chat_model(Settings.from_environment({}), effort="low")


@pytest.mark.parametrize("blank", ["", "   "])
def test_a_blank_key_on_hand_built_settings_is_refused_too(blank: str) -> None:
    """Settings built from the environment normalize a blank key to None; a
    Settings built directly may not, and the factory must not trust it."""
    settings = dataclasses.replace(Settings.from_environment({}), anthropic_api_key=blank)

    with pytest.raises(ModelUnavailable, match="ANTHROPIC_API_KEY"):
        chat_model(settings, effort="low")


def test_a_default_client_would_follow_the_shell_proxy(
    monkeypatch: pytest.MonkeyPatch, clean_routing_environment: None
) -> None:
    """The premise behind the pinning: the integration's own client honors the shell."""
    monkeypatch.setenv("HTTPS_PROXY", "http://proxy.example:8080")

    default = ChatAnthropic(
        model_name=MODEL, api_key=SecretStr("sk-test-value"), stop=None, timeout=None
    )

    assert default._client._client._trust_env is True
    assert any(mount is not None for mount in default._client._client._mounts.values())


@pytest.mark.parametrize("variable", sorted(REROUTING_VARIABLES))
def test_no_environment_variable_reroutes_a_request(
    variable: str, monkeypatch: pytest.MonkeyPatch, clean_routing_environment: None
) -> None:
    monkeypatch.setenv(variable, REROUTING_VARIABLES[variable])

    assert_pinned(chat_model(settings_with_key(), effort="low"))


def test_the_gateway_with_its_key_changes_neither_endpoint_nor_key(
    monkeypatch: pytest.MonkeyPatch, clean_routing_environment: None
) -> None:
    """Both gateway variables together is the setup the resolver prefers the gateway key in."""
    monkeypatch.setenv("LANGSMITH_GATEWAY", "true")
    monkeypatch.setenv("LANGSMITH_GATEWAY_API_KEY", "gateway-key")
    monkeypatch.setenv("LANGSMITH_API_KEY", "smith-key")

    assert_pinned(chat_model(settings_with_key(), effort="low"))


def test_the_client_carries_no_provider_side_tools_and_pins_its_limits() -> None:
    model = chat_model(settings_with_key(), effort="medium")

    assert model.model == MODEL
    assert model.mcp_servers is None
    assert model.betas is None
    assert model.thinking == {"type": "adaptive"}
    assert model.reasoning_effort == "medium"
    assert model.max_tokens == MAX_TOKENS
    assert model.default_request_timeout == TIMEOUT_SECONDS
    assert model.max_retries == 2
    assert model._client.max_retries == 2
    assert model._client.timeout == TIMEOUT_SECONDS


def test_the_key_never_appears_in_the_client_repr() -> None:
    model = chat_model(settings_with_key(), effort="low")

    assert "sk-test-value" not in repr(model)
    assert "sk-test-value" not in str(model)


# ---------------------------------------------------------------- tracing


def test_enforce_local_only_tracing_overrides_an_enabled_environment() -> None:
    environ = dict.fromkeys(HOSTED_TRACING_VARIABLES, "true") | dict.fromkeys(
        TRACE_PAYLOAD_VARIABLES, "false"
    )
    enforce_local_only_tracing(environ)

    assert all(environ[variable] == "false" for variable in HOSTED_TRACING_VARIABLES)
    assert all(environ[variable] == "true" for variable in TRACE_PAYLOAD_VARIABLES)


def test_the_tracer_detector_reads_true_when_a_tracer_is_attached() -> None:
    """Premise for every guard that asserts the detector reads False."""
    assert hosted_tracer_attached() is False
    with tracing_v2_enabled():
        assert hosted_tracer_attached() is True
    assert hosted_tracer_attached() is False


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
    assert hosted_tracer_attached() is True

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
