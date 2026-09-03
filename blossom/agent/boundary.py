"""Runtime backstop for the tool boundary.

``blossom/tools.py`` is the only place a framework tool object is constructed,
and every one it builds wraps a spec that passed the capability allowlist. That
covers tools this package creates. It does not cover tools that reach the graph
another way: a loader that turns an external server's tools into framework
tools, a prebuilt agent that injects its own, or anything added by a future
module that never went through the constructor.

This middleware closes that gap at the last moment a tool call can be stopped.
Every call is checked against the registry by name before it runs. A name the
registry does not know is refused with an error message the model can read,
and the tool itself is never invoked. The check is simple on purpose: a
registered name, and capabilities within the allowlist. It does not reason
about intent, because a boundary that can be argued with is not a boundary.

The two layers together are the guarantee. Construction keeps the set of tools
small and known; this backstop keeps a call from reaching anything outside that
set even if construction was bypassed. A test confines the middleware
constructor to this module.
"""

from collections.abc import Callable
from typing import Any

from langchain.agents.middleware import AgentMiddleware, ToolCallRequest, wrap_tool_call
from langchain_core.messages import ToolMessage
from langgraph.types import Command

from blossom.tools import ALLOWED_CAPABILITIES, TOOL_REGISTRY, ToolSpec

REGISTERED: dict[str, ToolSpec] = {spec.name: spec for spec in TOOL_REGISTRY}

ToolHandler = Callable[[ToolCallRequest], ToolMessage | Command[Any]]


def refusal(name: str, tool_call_id: str) -> ToolMessage:
    """The message the model receives instead of a result when a call is refused."""
    return ToolMessage(
        content=(
            f"blocked: {name!r} is not a registered tool. The only tools available "
            f"are {sorted(REGISTERED)}, and none of them sends anything."
        ),
        tool_call_id=tool_call_id,
        status="error",
    )


def is_permitted(name: str) -> bool:
    """True only for a registered tool whose capabilities are all allowed."""
    spec = REGISTERED.get(name)
    return spec is not None and spec.capabilities <= ALLOWED_CAPABILITIES


@wrap_tool_call
def tool_boundary(request: ToolCallRequest, handler: ToolHandler) -> ToolMessage | Command[Any]:
    """Refuse any tool call the registry does not know; pass the rest through."""
    name = str(request.tool_call["name"])
    if not is_permitted(name):
        return refusal(name, str(request.tool_call.get("id") or ""))
    return handler(request)


def boundary_middleware() -> AgentMiddleware[Any, Any]:
    """The middleware instance to pass to an agent or graph builder."""
    return tool_boundary
