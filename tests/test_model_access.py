# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Gerardo Bodegas Martinez
"""Properties of model access that hold before any model is called.

The API key is read from the environment and kept out of ``repr`` and ``str``
of the settings; hosted tracing for the model framework is off after startup
no matter which of its environment spellings a shell set; and the one model
client the package can construct sends to the public endpoint through an HTTP
client that ignores the environment, carries no provider-side tools, and
refuses to exist without a key.
"""

import ast
import base64
import dataclasses
import hashlib
import io
import json
import logging
import os
import pathlib
import re
import time
from collections.abc import Iterator
from typing import Any, get_args

import anthropic
import httpx
import pytest
from fastapi.testclient import TestClient
from langchain_anthropic import ChatAnthropic
from langchain_anthropic._client_utils import _get_default_httpx_client
from langchain_core.callbacks.manager import CallbackManager
from langchain_core.messages import HumanMessage
from langchain_core.tracers.context import tracing_v2_enabled
from langsmith import utils as langsmith_utils
from PIL import Image
from pydantic import SecretStr

from blossom import anthropic_client
from blossom.anthropic_client import (
    ENDPOINT,
    MAX_TOKENS,
    MODEL,
    TIMEOUT_SECONDS,
    Effort,
    ModelUnavailable,
    ServiceBusy,
    ServiceFailed,
    chat_model,
    service_failure,
)
from blossom.app import create_app
from blossom.settings import (
    ANTHROPIC_API_KEY_VARIABLE,
    HOSTED_TRACING_VARIABLES,
    TRACE_PAYLOAD_VARIABLES,
    Settings,
    enforce_local_only_tracing,
)
from blossom.stores.import_drafts import FAILURES
from tests.support import SAME_ORIGIN, fixture_settings, hosted_tracer_attached, trickling_answer

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
    assert model.max_retries == 0, "a plan run asks again itself, inside its time limit"
    assert model._client.max_retries == 0
    assert model._client.timeout == TIMEOUT_SECONDS


def test_the_guides_name_the_model_the_client_asks_for() -> None:
    """The architecture notes and the developer guide name the model the code calls, and
    no other."""
    assert MODEL == "claude-opus-5-5"
    root = pathlib.Path(__file__).parents[1]
    for guide in ("docs/architecture.md", "docs/development.md"):
        text = (root / guide).read_text(encoding="utf-8")
        assert set(re.findall(r"`(claude-opus-[0-9-]+)`", text)) == {MODEL}, guide


INTEGRATION_FALLBACK = 4_096
"""What the pinned integration sends for a model it has no profile for."""


@pytest.mark.parametrize("effort", ["high", "medium"], ids=["planner", "critic"])
def test_each_role_sends_the_cap_rather_than_the_integration_fallback(effort: Effort) -> None:
    model = chat_model(settings_with_key(), effort=effort)
    payload = model._get_request_payload([HumanMessage("Plan the evening for Wren.")])
    unpinned = ChatAnthropic(
        model_name=MODEL, api_key=SecretStr("sk-test-value"), stop=None, timeout=None
    )

    assert payload["max_tokens"] == MAX_TOKENS == 16_000
    assert unpinned.max_tokens == INTEGRATION_FALLBACK


def cap_notes() -> dict[str, str]:
    """The module's notes and the cap's own docstring, as the source holds them."""
    tree = ast.parse(pathlib.Path(anthropic_client.__file__).read_text(encoding="utf-8"))
    notes = {"module": ast.get_docstring(tree) or ""}
    for statement, after in zip(tree.body, tree.body[1:], strict=False):
        named = statement.target if isinstance(statement, ast.AnnAssign) else None
        if isinstance(named, ast.Name) and named.id == "MAX_TOKENS":
            assert isinstance(after, ast.Expr)
            assert isinstance(after.value, ast.Constant)
            notes["MAX_TOKENS"] = str(after.value.value)
    return notes


def where_model(where: str) -> str:
    """How each note names the model it has no profile for."""
    return f"``{MODEL}``" if where == "module" else "this model"


@pytest.mark.parametrize("where", ["module", "MAX_TOKENS"])
def test_the_notes_on_the_cap_name_the_fallback_and_the_model_maximum(where: str) -> None:
    """Both notes give the fallback the pinned integration sends and the model's own limit."""
    text = " ".join(cap_notes()[where].split())

    assert f"no profile for {where_model(where)}" in text
    assert f"fallback of {INTEGRATION_FALLBACK:,}" in text
    assert re.search(r"\bmodel\b[^,;.]*\b128,000\b", text), text
    assert "fallback of 128,000" not in text
    assert re.search(r"\bmodel\b[^,;.]*\b4,096\b", text) is None, text


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

    with TestClient(create_app(), headers=SAME_ORIGIN):
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

    with TestClient(create_app(), headers=SAME_ORIGIN):
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
    with TestClient(app, headers=SAME_ORIGIN):
        assert os.environ["LANGSMITH_TRACING"] == "false"


def status_error(code: int) -> anthropic.APIStatusError:
    request = httpx.Request("POST", f"{ENDPOINT}/v1/messages")
    return anthropic.APIStatusError(
        "refused", response=httpx.Response(code, request=request), body=None
    )


@pytest.mark.parametrize("code", [408, 409, 429, 500, 529])
def test_a_busy_or_failing_status_is_worth_asking_again(code: int) -> None:
    assert isinstance(service_failure(status_error(code)), ServiceBusy)


def test_a_connection_that_failed_or_timed_out_is_worth_asking_again() -> None:
    request = httpx.Request("POST", f"{ENDPOINT}/v1/messages")
    assert isinstance(service_failure(anthropic.APIConnectionError(request=request)), ServiceBusy)
    assert isinstance(service_failure(anthropic.APITimeoutError(request=request)), ServiceBusy)


@pytest.mark.parametrize("code", [400, 401, 403, 404, 422])
def test_a_refused_request_is_a_failure_not_asked_again(code: int) -> None:
    failure = service_failure(status_error(code))
    assert isinstance(failure, ServiceFailed)
    assert not isinstance(failure, ServiceBusy)


def test_an_error_the_service_did_not_raise_is_not_the_services() -> None:
    assert service_failure(ValueError("a fault in the caller")) is None


# ------------------------------------------------------------ the screenshot reader

PROMPT_V1B_SHA256 = "e85ede52600be9d0276324198f37cb1e1737e81664b58e586ccfd21aafebe2fc"
"""The system prompt the reader was measured with: prompt v1b, its schema as JSON text."""

SDK_LOGGERS = (
    "anthropic",
    "anthropic._base_client",
    "httpx",
    "httpcore",
    "httpcore.connection",
    "httpcore.http11",
)
"""The SDK's and its HTTP clients' loggers, a parent and its children."""

AN_ANSWER: dict[str, Any] = {
    "image_kind": "homework_list",
    "active_student": {"name": "Wren", "uncertain": None},
    "headings": [{"text": "Monday 10/5/2026", "y": 120, "uncertain": None}],
    "cards": [],
    "report": None,
}


def synthetic_png() -> bytes:
    buffer = io.BytesIO()
    Image.new("RGB", (40, 30), (20, 120, 200)).save(buffer, format="PNG")
    return buffer.getvalue()


def message(text: str | None, *, stop: str = "end_turn") -> httpx.Response:
    """A Messages API answer as the service sends it, with one text block or none."""
    content = [] if text is None else [{"type": "thinking", "thinking": "", "signature": ""}]
    content += [] if text is None else [{"type": "text", "text": text}]
    return httpx.Response(
        200,
        json={
            "id": "msg_synthetic",
            "type": "message",
            "model": MODEL,
            "content": content,
            "stop_reason": stop,
            "stop_sequence": None,
            "usage": {"input_tokens": 1500, "output_tokens": 300},
        },
    )


class Service:
    """A mock transport that answers each request from a queue and keeps what was sent."""

    def __init__(self, *answers: httpx.Response | Exception) -> None:
        self.answers = list(answers)
        self.sent: list[httpx.Request] = []
        self.transport = httpx.MockTransport(self.handle)

    def handle(self, request: httpx.Request) -> httpx.Response:
        self.sent.append(request)
        answer = self.answers.pop(0)
        if isinstance(answer, Exception):
            raise answer
        return answer


def read(
    service: Service, png: bytes | None = None, timeout: float = 30.0
) -> anthropic_client.ScreenshotAnswer:
    """One reading through the real SDK on the mock transport, so nothing leaves the machine."""
    return anthropic_client.read_screenshot(
        settings_with_key(),
        synthetic_png() if png is None else png,
        timeout=timeout,
        transport=service.transport,
    )


def test_a_reading_sends_the_request_the_reader_was_measured_with() -> None:
    png = synthetic_png()
    service = Service(message(json.dumps(AN_ANSWER)))

    read(service, png)

    [sent] = service.sent
    body = json.loads(sent.content)
    assert sent.method == "POST"
    assert str(sent.url) == f"{ENDPOINT}/v1/messages"
    assert set(body) == {"model", "max_tokens", "system", "output_config", "messages"}
    assert body["model"] == MODEL
    assert body["max_tokens"] == 6_000
    assert body["output_config"] == {"effort": "low"}
    assert hashlib.sha256(body["system"].encode("utf-8")).hexdigest() == PROMPT_V1B_SHA256
    assert body["messages"] == [
        {
            "role": "user",
            "content": [
                {
                    "type": "image",
                    "source": {
                        "type": "base64",
                        "media_type": "image/png",
                        "data": base64.standard_b64encode(png).decode("ascii"),
                    },
                },
                {"type": "text", "text": "Read this screenshot."},
            ],
        }
    ]
    assert "anthropic-beta" not in sent.headers
    assert "x-stainless-raw-response" not in sent.headers


@pytest.mark.parametrize("variable", ["ANTHROPIC_BASE_URL", "ANTHROPIC_API_URL"])
def test_a_reading_goes_to_the_public_endpoint_whatever_the_shell_names(
    variable: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv(variable, REROUTING_VARIABLES[variable])
    service = Service(message(json.dumps(AN_ANSWER)))

    read(service)

    assert str(service.sent[0].url) == f"{ENDPOINT}/v1/messages"


def test_the_reader_client_ignores_the_environment_and_never_retries_on_its_own(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    for variable, value in REROUTING_VARIABLES.items():
        monkeypatch.setenv(variable, value)

    with anthropic_client._reader_client("sk-test-value", 12.5, None) as client:
        assert str(client.base_url).rstrip("/") == ENDPOINT
        assert client.max_retries == 0
        assert client.timeout == 12.5
        assert client._client._trust_env is False
        assert client._client._mounts == {}


def test_a_reading_sends_the_timeout_it_is_given_with_its_request() -> None:
    """The request carries the call's timeout for connecting, reading, writing and the pool.
    The setting is checked here; the trickling tests check time spent over a mock service."""
    service = Service(message(json.dumps(AN_ANSWER)))

    read(service, timeout=7.5)

    assert service.sent[0].extensions["timeout"] == dict.fromkeys(
        ("connect", "read", "write", "pool"), 7.5
    )


@pytest.mark.parametrize("timeout", [0.0, -1.0])
def test_a_reading_with_no_time_left_is_not_sent(timeout: float) -> None:
    service = Service()

    with pytest.raises(ValueError, match="positive"):
        read(service, timeout=timeout)

    assert service.sent == []


def test_a_reading_needs_a_key_and_sends_nothing_without_one() -> None:
    service = Service()

    with pytest.raises(ModelUnavailable, match="ANTHROPIC_API_KEY"):
        anthropic_client.read_screenshot(
            Settings.from_environment({}),
            synthetic_png(),
            timeout=30.0,
            transport=service.transport,
        )

    assert service.sent == []


@pytest.mark.parametrize(
    "text",
    [
        json.dumps(AN_ANSWER),
        f"  {json.dumps(AN_ANSWER, indent=2)}\n",
        f"```json\n{json.dumps(AN_ANSWER)}\n```",
        f"```\n{json.dumps(AN_ANSWER)}\n```",
    ],
    ids=["plain", "spaced", "json-fence", "bare-fence"],
)
def test_an_answer_parses_with_or_without_a_code_fence(text: str) -> None:
    answer = read(Service(message(text)))

    assert answer.failure is None
    assert answer.answer == AN_ANSWER
    assert (answer.input_tokens, answer.output_tokens) == (1500, 300)


@pytest.mark.parametrize(
    ("stop", "failure"),
    [
        ("max_tokens", "cut_off"),
        ("model_context_window_exceeded", "cut_off"),
        ("refusal", "model_refused"),
    ],
)
def test_a_stopped_or_refused_answer_is_a_failed_read_even_when_it_parses(
    stop: str, failure: str
) -> None:
    answer = read(Service(message(json.dumps(AN_ANSWER), stop=stop)))

    assert answer.failure == failure
    assert answer.answer is None
    assert (answer.input_tokens, answer.output_tokens) == (1500, 300)


@pytest.mark.parametrize(
    "text",
    [
        f"Here is what the screenshot shows: {json.dumps(AN_ANSWER)}",
        "",
        '```json\n{"image_kind": \n```',
        "```",
        "[" * 100_000,
        "1" * 5_000,
        None,
    ],
    ids=["prose", "empty", "broken-fence", "fence-alone", "too-deep", "long-number", "no-text"],
)
def test_an_answer_that_is_not_json_is_a_failed_read(text: str | None) -> None:
    answer = read(Service(message(text)))

    assert answer.failure == "not_json"
    assert answer.answer is None


BUSY = [
    *(httpx.Response(code, json={"type": "error"}) for code in (408, 409, 429, 500, 503, 529)),
    httpx.ConnectError("synthetic refusal to connect"),
    httpx.ReadTimeout("synthetic timeout"),
]


@pytest.mark.parametrize(
    "outcome", BUSY, ids=["408", "409", "429", "500", "503", "529", "connect", "timeout"]
)
def test_a_busy_service_is_named_busy_and_asked_once(outcome: httpx.Response | Exception) -> None:
    service = Service(outcome)

    answer = read(service)

    assert answer.failure == "service_busy"
    assert len(service.sent) == 1
    assert (answer.answer, answer.input_tokens, answer.output_tokens) == (None, 0, 0)


@pytest.mark.parametrize("code", [400, 401, 403, 404, 413, 422])
def test_a_refused_request_is_named_refused_and_asked_once(code: int) -> None:
    service = Service(httpx.Response(code, json={"type": "error"}))

    answer = read(service)

    assert answer.failure == "service_refused"
    assert len(service.sent) == 1


@pytest.mark.parametrize(
    "body", [b"Sentinel-Body-B1 is not JSON", b'{"content": "\xff"}'], ids=["not-json", "not-utf-8"]
)
def test_a_response_the_sdk_cannot_decode_is_named_refused(body: bytes) -> None:
    """The SDK raises the decoder's own error for such a body, which holds the body."""
    service = Service(
        httpx.Response(200, headers={"content-type": "application/json"}, content=body)
    )

    answer = read(service)

    assert answer.failure == "service_refused"
    assert "Sentinel" not in repr(answer)


def test_every_failure_a_reading_names_is_one_the_draft_store_keeps() -> None:
    named = {"service_busy", "service_refused", "model_refused", "cut_off", "not_json"}

    assert named <= set(FAILURES)
    assert set(get_args(anthropic_client.ReadFailure)) == named


def test_the_model_client_loggers_are_held_above_debug_from_startup() -> None:
    loggers = [logging.getLogger(name) for name in SDK_LOGGERS]
    levels = [logger.level for logger in loggers]
    try:
        for logger in loggers:
            logger.setLevel(logging.DEBUG)
        with TestClient(create_app(fixture_settings()), headers=SAME_ORIGIN):
            held = {logger.name: logger.getEffectiveLevel() for logger in loggers}
    finally:
        for logger, level in zip(loggers, levels, strict=True):
            logger.setLevel(level)

    assert all(level > logging.DEBUG for level in held.values()), held


def test_no_screenshot_answer_or_error_body_reaches_a_log_with_the_sdk_at_debug(
    caplog: pytest.LogCaptureFixture, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``ANTHROPIC_LOG=debug`` and the SDK's loggers at DEBUG with a root handler before the
    app starts, as a household that turned its logging up would have them. The SDK reads the
    variable at its import, so its levels are set here as that read sets them."""
    monkeypatch.setenv("ANTHROPIC_LOG", "debug")
    png = synthetic_png()
    image = base64.standard_b64encode(png).decode("ascii")
    named = dict(AN_ANSWER, active_student={"name": "Sentinel-N1", "uncertain": "Sentinel-U2"})
    refused = {"type": "error", "error": {"type": "invalid_request", "message": "Sentinel-E3"}}
    service = Service(
        message(json.dumps(named)),
        httpx.Response(400, json=refused),
        httpx.Response(200, headers={"content-type": "application/json"}, content=b"Sentinel-B4"),
        message(f"Sentinel-P5 {json.dumps(named)}"),
    )
    loggers = [logging.getLogger(name) for name in SDK_LOGGERS]
    levels = [logger.level for logger in loggers]
    try:
        for logger in loggers:
            logger.setLevel(logging.DEBUG)
        with (
            TestClient(create_app(fixture_settings()), headers=SAME_ORIGIN),
            caplog.at_level(logging.DEBUG),
        ):
            answers = [read(service, png) for _ in range(4)]
    finally:
        for logger, level in zip(loggers, levels, strict=True):
            logger.setLevel(level)

    assert [answer.failure for answer in answers] == [
        None,
        "service_refused",
        "service_refused",
        "not_json",
    ]
    assert answers[0].answer["active_student"]["name"] == "Sentinel-N1"
    assert "Sentinel" not in caplog.text
    assert image not in caplog.text
    assert image[:64] not in caplog.text
    assert not [
        record.name
        for record in caplog.records
        if record.name.split(".")[0] in {"anthropic", "httpx", "httpcore"}
        and record.levelno <= logging.DEBUG
    ]


@pytest.mark.parametrize(
    "fence", ["```python\n", "```JSON\n", "```jsonc\n"], ids=["python", "upper-case", "jsonc"]
)
def test_only_a_json_fence_is_stripped(fence: str) -> None:
    """The answer is read as the tested reader read it: a fence named anything but json is
    left in place, and the text is then not JSON."""
    answer = read(Service(message(f"{fence}{json.dumps(AN_ANSWER)}\n```")))

    assert answer.failure == "not_json"


def trickling(seconds: float) -> httpx.MockTransport:
    """A service whose answer trickles in for about ``seconds``."""
    return trickling_answer(AN_ANSWER, seconds)


def test_a_trickling_answer_ends_busy_by_the_calls_time_plus_one_read() -> None:
    """A body still arriving when the call's time is up ends the call as a busy service,
    within one more piece, and nothing escapes."""
    started = time.monotonic()

    answer = anthropic_client.read_screenshot(
        settings_with_key(), synthetic_png(), timeout=0.3, transport=trickling(3.0)
    )

    assert time.monotonic() - started < 0.3 + 0.3
    assert answer == anthropic_client.ScreenshotAnswer(None, "service_busy", 0, 0)


def test_an_answer_that_arrives_in_time_is_read_whole_through_the_time_check() -> None:
    """An answer that trickles in for about 0.3 seconds under a 5 second call reads in full."""
    answer = anthropic_client.read_screenshot(
        settings_with_key(), synthetic_png(), timeout=5.0, transport=trickling(0.3)
    )

    assert answer.failure is None
    assert answer.answer == AN_ANSWER


def test_the_reader_client_bounds_its_own_transport_when_none_is_given(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """In use no transport is given: the client builds its own, environment trust off, and
    wraps it in the call's time check. Nothing is sent; the client is only built."""
    for variable, value in REROUTING_VARIABLES.items():
        monkeypatch.setenv(variable, value)

    with anthropic_client._reader_client("sk-test-value", 12.5, None) as client:
        outer = client._client._transport
        assert isinstance(outer, anthropic_client._InTime)
        assert type(outer._inner) is httpx.HTTPTransport
        assert client._client._mounts == {}
        assert client._client._trust_env is False
