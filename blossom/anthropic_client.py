"""The one place a model client is constructed.

Every model call in this system goes through ``chat_model``. The import
allowlist in ``tests/test_capability_boundaries.py`` confines both the model
framework's Anthropic integration and the SDK beneath it to this file, so a
route or a graph node cannot construct a client of its own without a
reviewable edit to that allowlist.

Construction pins what the integration would otherwise read from the
environment. Left to itself, ``ChatAnthropic`` resolves its endpoint from
``ANTHROPIC_API_URL`` or ``ANTHROPIC_BASE_URL``, an HTTP proxy from
``ANTHROPIC_PROXY``, and a hosted gateway from ``LANGSMITH_GATEWAY``, all at
construction time. Here the key comes from ``Settings``, the endpoint is the
public API, and there is no proxy, so no variable in a shell can reroute a
request that carries a student's assignments. Two defaults that would surprise
are pinned as well: the integration takes ``max_tokens`` from the model
profile, which is 128,000 for the models it names, and sends requests with no
timeout.

Nothing here binds a tool, names a server-side tool, or lists a beta. Tools
reach a model only through ``blossom.agent`` and its boundary; a provider-run
tool would be a sending path the registry cannot see.
"""

from typing import Final, Literal

from langchain_anthropic import ChatAnthropic
from pydantic import SecretStr

from blossom.settings import Settings

MODEL: Final = "claude-opus-5"
"""One model for every role. Depth is tuned per role with ``effort``, which
Anthropic recommends over a second, cheaper model."""

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
    if not key:
        msg = "no model can be constructed: ANTHROPIC_API_KEY is not set"
        raise ModelUnavailable(msg)
    # The integration declares its fields under aliases (``model_name`` for
    # ``model``, ``max_tokens_to_sample`` for ``max_tokens``); the alias is what
    # the type checker accepts, so the alias is what is written here. It also
    # reads the stop-sequence field as required, so its absence is spelled out.
    return ChatAnthropic(
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
