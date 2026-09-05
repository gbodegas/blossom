"""The one place a model client is constructed.

Every model call in this system goes through ``chat_model``. The import
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
at. Here the key comes from ``Settings``, the endpoint is the public API, and
the HTTP clients are built in this file with environment trust switched off,
so no variable in a shell can change where a prompt is sent or which
certificates the connection trusts. Two defaults that would surprise are
pinned as well: the integration takes ``max_tokens`` from the model profile,
which is 128,000 for ``claude-opus-5``, and sends requests with no timeout.

Nothing here binds a tool, names a server-side tool, or lists a beta. Tools
reach a model only through ``blossom.agent`` and its boundary; a provider-run
tool would be a sending path the registry cannot see.
"""

from functools import cached_property
from typing import Final, Literal

import anthropic
import httpx
from langchain_anthropic import ChatAnthropic
from pydantic import SecretStr

from blossom.settings import Settings

MODEL: Final = "claude-opus-5"
"""One model for every role. Depth is tuned per role with ``effort`` rather
than with a second, cheaper model, which Anthropic suggests measuring against
before adding."""

ENDPOINT: Final = "https://api.anthropic.com"
"""The public API. Fixed in code so that no environment variable decides where
a prompt is sent."""

MAX_TOKENS: Final = 16_000
"""Room for a plan or a verdict with adaptive thinking, far below the profile
default of 128,000 that would otherwise apply."""

TIMEOUT_SECONDS: Final = 120.0
"""Per request. The web app awaits these calls, so a hung request must fail
inside the time a page is willing to wait."""

Effort = Literal["low", "medium", "high"]
"""The effort levels a role may ask for. The two above ``high`` are for work
harder than planning one student's week."""


class ModelUnavailable(RuntimeError):
    """Raised when settings carry no API key. Nothing calls a model without one."""


MISSING_KEY: Final = "no model can be constructed: ANTHROPIC_API_KEY is not set"


def model_configured(settings: Settings) -> bool:
    """True when settings carry a key that is not blank.

    The one definition of "there is a model", used by the seam that refuses to
    build without one and by the pages that say whether a run can start.
    """
    return bool((settings.anthropic_api_key or "").strip())


class PinnedChatAnthropic(ChatAnthropic):
    """``ChatAnthropic`` whose HTTP clients ignore the environment.

    The integration exposes no way to supply an HTTP client, and the SDK's
    default client honors the proxy and certificate variables. These two
    properties are the integration's own, rebuilt around clients constructed
    here with ``trust_env`` off and no proxy mounts.
    """

    @cached_property
    def _client(self) -> anthropic.Client:
        http_client = httpx.Client(timeout=TIMEOUT_SECONDS, trust_env=False)
        return anthropic.Client(**self._client_params, http_client=http_client)

    @cached_property
    def _async_client(self) -> anthropic.AsyncClient:
        http_client = httpx.AsyncClient(timeout=TIMEOUT_SECONDS, trust_env=False)
        return anthropic.AsyncClient(**self._client_params, http_client=http_client)


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
    return PinnedChatAnthropic(
        model_name=MODEL,
        api_key=SecretStr(key),
        base_url=ENDPOINT,
        anthropic_proxy=None,
        max_tokens_to_sample=MAX_TOKENS,
        timeout=TIMEOUT_SECONDS,
        max_retries=2,
        stop=None,
        effort=effort,
        thinking={"type": "adaptive"},
    )
