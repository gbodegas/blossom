# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Gerardo Bodegas Martinez
"""The one place a model client is constructed.

Every planner call in this system goes through ``chat_model``, and every
screenshot reading through ``read_screenshot``. The import
allowlist in ``tests/test_capability_boundaries.py`` confines the model
framework's Anthropic integration, the SDK beneath it, and the HTTP library
beneath that to this file, so a route or a graph node cannot construct a client
of its own without a reviewable edit to that allowlist.

Construction pins what the integration and the SDK would otherwise read from
the environment. Left to itself, ``ChatAnthropic`` resolves its endpoint from
``ANTHROPIC_API_URL`` or ``ANTHROPIC_BASE_URL``, a proxy from
``ANTHROPIC_PROXY``, and a hosted gateway from ``LANGSMITH_GATEWAY``; the
SDK's default HTTP client then mounts whatever ``HTTP_PROXY``, ``HTTPS_PROXY``,
and ``ALL_PROXY`` name and trusts whatever certificate ``SSL_CERT_FILE`` points
at, and the SDK adds the headers ``ANTHROPIC_CUSTOM_HEADERS`` lists after its
key and API version. Here the key comes from ``Settings``, the endpoint is the
public API, the HTTP clients are built in this file with environment trust
switched off, and each SDK client keeps only the headers passed to it, so no
variable in a shell can change where a prompt is sent, which certificates the
connection trusts, or which headers a request carries. Two defaults that would surprise are
pinned as well. The integration takes ``max_tokens`` from its model profile,
and the pinned release has no profile for ``claude-opus-5-5``, so it would
send its fallback of 4,096, well below the model's own limit of 128,000. It
also sends requests with no timeout.

Nothing here binds a tool, names a server-side tool, or lists a beta. Tools
reach a model only through ``blossom.agent`` and its boundary; a provider-run
tool would be a sending path the registry cannot see.

The screenshot reader is a direct SDK client, outside the model framework, so
no callback, and so no tracer, sees a screenshot or what was read from it.
"""

import base64
import json
import logging
import time
from collections.abc import Iterator, Mapping
from functools import cached_property
from typing import Any, Final, Literal, NamedTuple

import anthropic
import httpx
from langchain_anthropic import ChatAnthropic
from pydantic import SecretStr

from blossom.screenshot_prompt import SYSTEM_PROMPT, USER_TEXT
from blossom.settings import Settings

MODEL: Final = "claude-opus-5-5"
"""One model for every role. Depth is tuned per role with ``effort`` rather
than with a second, cheaper model, which Anthropic suggests measuring against
before adding."""

ENDPOINT: Final = "https://api.anthropic.com"
"""The public API. Fixed in code so that no environment variable decides where
a prompt is sent."""

MAX_TOKENS: Final = 16_000
"""Room for a plan or a verdict with adaptive thinking. Left unset, the pinned
integration would send its fallback of 4,096, since it has no profile for this
model; the model itself allows up to 128,000."""

TIMEOUT_SECONDS: Final = 120.0
"""Per request, a backstop. A plan run holds each request to what is left of its own
time limit, which is shorter."""

BUSY_STATUSES: Final = frozenset({408, 409, 429})
"""Answers that say the service is busy rather than that the request is wrong; any
status from 500 up says the same."""

Effort = Literal["low", "medium", "high"]
"""The effort levels a role may ask for. The two above ``high`` are for work
harder than planning one student's week."""


class ModelUnavailable(RuntimeError):
    """Raised when settings carry no API key. Nothing calls a model without one."""


class ServiceFailed(RuntimeError):
    """A model call the service refused or never answered."""


class ServiceBusy(ServiceFailed):
    """A model call the service could not take just now, which may work if asked again."""


def _service_is_busy(error: anthropic.APIError) -> bool:
    """True for a connection that failed or timed out, or a busy or failing status: the one
    definition of a refusal worth asking again."""
    if isinstance(error, anthropic.APIConnectionError):
        return True
    if isinstance(error, anthropic.APIStatusError):
        return error.status_code in BUSY_STATUSES or error.status_code >= 500
    return False


def service_failure(error: Exception) -> ServiceFailed | None:
    """What ``error`` from a model call says about the service: busy, failed, or nothing.

    A connection that failed or timed out, and a busy or failing status, are worth
    asking again; any other refusal from the service is not. ``None`` for an error
    the service did not raise.
    """
    if isinstance(error, anthropic.APIError):
        return ServiceBusy(str(error)) if _service_is_busy(error) else ServiceFailed(str(error))
    return None


MISSING_KEY: Final = "no model can be constructed: ANTHROPIC_API_KEY is not set"


def model_configured(settings: Settings) -> bool:
    """True when settings carry a key that is not blank.

    The one definition of "there is a model", used by the seam that refuses to
    build without one and by the pages that say whether a run can start.
    """
    return bool((settings.anthropic_api_key or "").strip())


def _with_only_these_headers[C: (anthropic.Client, anthropic.AsyncClient)](
    client: C, headers: Mapping[str, str]
) -> C:
    """``client`` with exactly ``headers`` as its own. The SDK adds the lines of
    ``ANTHROPIC_CUSTOM_HEADERS`` to them as it builds a client, after its key and API
    version, so the variable could add a beta or replace the key. ``copy`` and
    ``with_options`` build a client again and read the variable again, so this file
    calls neither."""
    client._custom_headers = dict(headers)
    return client


class PinnedChatAnthropic(ChatAnthropic):
    """``ChatAnthropic`` whose HTTP clients ignore the environment.

    The integration exposes no way to supply an HTTP client, and the SDK's
    default client honors the proxy and certificate variables. These two
    properties are the integration's own, rebuilt around clients constructed
    here with ``trust_env`` off and no proxy mounts, each keeping only the
    headers the integration passes.
    """

    @cached_property
    def _client(self) -> anthropic.Client:
        http_client = httpx.Client(timeout=TIMEOUT_SECONDS, trust_env=False)
        client = anthropic.Client(**self._client_params, http_client=http_client)
        return _with_only_these_headers(client, self._client_params["default_headers"])

    @cached_property
    def _async_client(self) -> anthropic.AsyncClient:
        http_client = httpx.AsyncClient(timeout=TIMEOUT_SECONDS, trust_env=False)
        client = anthropic.AsyncClient(**self._client_params, http_client=http_client)
        return _with_only_these_headers(client, self._client_params["default_headers"])


def chat_model(settings: Settings, *, effort: Effort) -> ChatAnthropic:
    """Construct the model client for one role.

    ``effort`` is the only thing that varies between roles. Adaptive thinking
    is passed explicitly rather than left to the integration, which would
    otherwise ask for summarized reasoning and store it in every message; the
    reasoning summary is not part of the record this system keeps.
    """
    # Settings built from the environment normalize a blank key to None; a
    # Settings built by hand may carry an empty or blank string. Both are
    # refused, so no client is ever constructed with an empty key.
    key = (settings.anthropic_api_key or "").strip()
    if not model_configured(settings):
        raise ModelUnavailable(MISSING_KEY)
    # The integration declares its fields under aliases (``model_name`` for
    # ``model``, ``max_tokens_to_sample`` for ``max_tokens``); the alias is what
    # the type checker accepts, so the alias is what is written here. It also
    # reads the stop-sequence field as required, so its absence is spelled out.
    # The client never retries on its own: a plan run asks again itself, inside its
    # time limit, so a retry never starts a fresh wait.
    return PinnedChatAnthropic(
        model_name=MODEL,
        api_key=SecretStr(key),
        base_url=ENDPOINT,
        anthropic_proxy=None,
        max_tokens_to_sample=MAX_TOKENS,
        timeout=TIMEOUT_SECONDS,
        max_retries=0,
        stop=None,
        effort=effort,
        thinking={"type": "adaptive"},
    )


READER_MAX_TOKENS: Final = 6_000
"""Room for one screenshot's answer, thinking included, as the reader was tested. An answer
stopped at it is a failed read."""

ENDED_EARLY: Final = frozenset({"max_tokens", "model_context_window_exceeded"})
"""Stop reasons that end an answer before the model finished it. Such an answer may be
partial, so it is a failed read even when its text parses."""

READER_EFFORT: Final = "low"
"""The least thinking this model allows: its thinking can't be switched off."""

MODEL_CLIENT_LOGGERS: Final = ("anthropic", "httpx", "httpcore")
"""The loggers the SDK and its HTTP clients write through, each with its modules' beneath."""

ReadFailure = Literal["service_busy", "service_refused", "model_refused", "cut_off", "not_json"]
"""How one reading call can fail, named as the import drafts store keeps failures."""


class ScreenshotAnswer(NamedTuple):
    """One reading call's outcome: the answer as parsed, or how it failed, and its tokens.

    ``answer`` is the parsed JSON before any validation, and means something only when
    ``failure`` is ``None``. Nothing here holds the text as sent, a request or an error.
    """

    answer: Any
    failure: ReadFailure | None
    input_tokens: int
    output_tokens: int


def quiet_the_model_client() -> None:
    """Hold the SDK's and its HTTP clients' loggers above DEBUG, where the SDK logs a whole
    request, a screenshot's bytes and the prompt included. Every one of their loggers is
    held, since a record passes a parent's handlers without its level."""
    for name in list(logging.root.manager.loggerDict):
        if name.startswith(tuple(f"{root}." for root in MODEL_CLIENT_LOGGERS)):
            logging.getLogger(name).setLevel(logging.INFO)
    for root in MODEL_CLIENT_LOGGERS:
        logging.getLogger(root).setLevel(logging.INFO)


class _InTime(httpx.BaseTransport):
    """A transport that ends a call's answer once the call's time is up. It sends the request
    it is given, unchanged. Each piece of the answer's body that arrives after ``ends`` raises
    a read timeout, so a body that trickles ends within one read of the call's time. Connecting,
    sending the request and waiting for the answer's status line and headers are not checked
    here: they rely on each network operation's own timeout."""

    def __init__(self, inner: httpx.BaseTransport, ends: float) -> None:
        self._inner = inner
        self._ends = ends

    def handle_request(self, request: httpx.Request) -> httpx.Response:
        response = self._inner.handle_request(request)
        body = response.stream
        if not isinstance(body, httpx.SyncByteStream):
            message = "the reader's transport answers in pieces read in a thread"
            raise TypeError(message)
        return httpx.Response(
            status_code=response.status_code,
            headers=response.headers,
            stream=_Until(body, self._ends, request),
            extensions=response.extensions,
        )

    def close(self) -> None:
        self._inner.close()


class _Until(httpx.SyncByteStream):
    """An answer's body, read piece by piece until its call's time is up."""

    def __init__(self, body: httpx.SyncByteStream, ends: float, request: httpx.Request) -> None:
        self._body = body
        self._ends = ends
        self._request = request

    def __iter__(self) -> Iterator[bytes]:
        for piece in self._body:
            if time.monotonic() > self._ends:
                message = "the call's time is up"
                raise httpx.ReadTimeout(message, request=self._request)
            yield piece

    def close(self) -> None:
        self._body.close()


def _reader_client(
    key: str, timeout: float, transport: httpx.BaseTransport | None
) -> anthropic.Anthropic:
    """The reader's own client: the public endpoint, no environment trust, no headers but
    the SDK's own, no retry of its own, and the timeout it is given for each network
    operation. Through ``_InTime`` the
    answer's body is also checked against the call's time; the connection, the request and
    the answer's head are bounded only by each operation's timeout."""
    inner = httpx.HTTPTransport(trust_env=False) if transport is None else transport
    http_client = httpx.Client(
        timeout=timeout,
        trust_env=False,
        transport=_InTime(inner, time.monotonic() + timeout),
    )
    client = anthropic.Anthropic(
        api_key=key, base_url=ENDPOINT, max_retries=0, timeout=timeout, http_client=http_client
    )
    return _with_only_these_headers(client, {})


def read_screenshot(
    settings: Settings,
    png: bytes,
    *,
    timeout: float,
    transport: httpx.BaseTransport | None = None,
) -> ScreenshotAnswer:
    """Send one prepared PNG to be read, once, and parse the answer as the reader was tested.

    Every error that could carry the request, the answer or a response body is named by a
    failure kind alone, and nothing keeps a reference to it once its handler ends.
    ``transport`` replaces the network for a test.
    """
    key = (settings.anthropic_api_key or "").strip()
    if not model_configured(settings):
        raise ModelUnavailable(MISSING_KEY)
    if not timeout > 0:
        message = "a reading needs a positive timeout"
        raise ValueError(message)
    image = base64.standard_b64encode(png).decode("ascii")
    failure: ReadFailure | None = None
    with _reader_client(key, timeout, transport) as client:
        try:
            response = client.messages.create(
                model=MODEL,
                max_tokens=READER_MAX_TOKENS,
                system=SYSTEM_PROMPT,
                output_config={"effort": READER_EFFORT},
                messages=[
                    {
                        "role": "user",
                        "content": [
                            {
                                "type": "image",
                                "source": {
                                    "type": "base64",
                                    "media_type": "image/png",
                                    "data": image,
                                },
                            },
                            {"type": "text", "text": USER_TEXT},
                        ],
                    }
                ],
            )
        except anthropic.APIError as error:
            failure = "service_busy" if _service_is_busy(error) else "service_refused"
        except (ValueError, RecursionError):
            # The SDK decodes a response body with the JSON decoder and raises its errors
            # unwrapped; a decode error holds the body it couldn't read.
            failure = "service_refused"
    if failure is not None:
        return ScreenshotAnswer(None, failure, 0, 0)
    return _answer_of(response)


def _answer_of(response: object) -> ScreenshotAnswer:
    """The parsed answer of a response, or why there is none: a refusal, or an answer that
    ended early at the token limit or the context window, fails even when its text parses."""
    usage = getattr(response, "usage", None)
    tokens = (
        _count(getattr(usage, "input_tokens", None)),
        _count(getattr(usage, "output_tokens", None)),
    )
    stop = getattr(response, "stop_reason", None)
    content = getattr(response, "content", None)
    text = next(
        (
            getattr(block, "text", None)
            for block in (content if isinstance(content, list) else [])
            if getattr(block, "type", None) == "text"
        ),
        None,
    )
    failure: ReadFailure | None = None
    answer: Any = None
    if stop == "refusal":
        failure = "model_refused"
    elif stop in ENDED_EARLY:
        failure = "cut_off"
    elif not isinstance(text, str):
        failure = "not_json"
    else:
        try:
            answer = json.loads(_json_text(text))
        except (ValueError, RecursionError):
            failure = "not_json"
    return ScreenshotAnswer(None if failure else answer, failure, *tokens)


def _json_text(text: str) -> str:
    """The answer's JSON: the text itself, or the inside of one code fence around it."""
    stripped = text.strip()
    if stripped.startswith("```") and stripped.endswith("```"):
        inner = stripped[3:-3]
        if inner.startswith("json"):
            inner = inner[4:]
        return inner.strip()
    return stripped


def _count(value: object) -> int:
    """A token count as the service reported it, or 0 when it reported none."""
    return value if isinstance(value, int) and not isinstance(value, bool) and value >= 0 else 0
