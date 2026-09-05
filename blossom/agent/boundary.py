"""Runtime backstop for the tool boundary.

``blossom/tools.py`` is the only constructor of framework tool objects this
package provides, and it builds only for entries of its registry, checked by
identity. That covers tools this package creates. It does not cover
tools that reach an agent another way: a loader that turns an external server's
tools into framework objects, a prebuilt agent's own tools, a plain function
handed to a tool node, or a provider-executed tool passed to the model as a
dictionary.

This middleware closes those gaps at the two points the framework exposes.

Before each model call, every tool about to be bound is checked by identity
against the objects ``blossom/tools.py`` built. A foreign object, or a
dictionary describing a provider-executed tool, stops the run with
``ForeignToolError`` rather than being offered to the model. This is where a
tool that never went through the constructor is caught, including one that
copies a registered name.

Before each client-executed tool call, the requested tool object is checked the
same way: it must be one the constructor built, its name must match the call,
and its spec must still be within the allowlist. Anything else is refused with
an error message the model can read, and the tool is never invoked.

The check is simple on purpose: identity, name, allowlist. It does not reason
about intent, because a boundary that can be argued with is not a boundary.
Both hooks have synchronous and asynchronous forms, since the web app drives
the graph asynchronously.

Two facts about the framework decide how this middleware is attached. Wrap
hooks compose with the first middleware in the list outermost, so a middleware
listed after this one runs inside it and could hand the tool node a different
tool after the check has passed; ``middleware_stack`` places this middleware
last and is the only sanctioned way to assemble an agent's middleware list,
which a source scan in the tests enforces. And once any ``wrap_tool_call``
middleware is attached, the framework defers its own name check until the
handler runs and hands the hook ``None`` for a name it does not know, so a
middleware that never calls the handler never meets that check; this is why
``is_permitted_call`` requires a tool object and never trusts a name alone.

Before each model call the request's settings are checked as well as its
tools: a middleware outside this one can rewrite the settings the model is
bound with, and keys such as ``tools`` or ``mcp_servers`` there would hand the
model a tool the registry never saw.

The identity registry it consults is a record ``blossom/tools.py`` keeps for
itself. It tells that module's objects from foreign ones. It is not a defense
against code inside this package that edits the registry or a built object,
which review is for; the middleware trusts the package it belongs to.

Outside this middleware's reach: tools a model provider executes on its own
side because of how the model client itself was constructed. That is governed
by confining the model client's construction, not by this module.
"""

from collections.abc import Awaitable, Callable, Mapping
from typing import Any, Final

from langchain.agents.middleware import (
    AgentMiddleware,
    ExtendedModelResponse,
    ModelRequest,
    ModelResponse,
    ToolCallRequest,
)
from langchain_core.messages import AIMessage, ToolMessage
from langgraph.types import Command

from blossom.tools import ALLOWED_CAPABILITIES, TOOL_REGISTRY, ToolSpec, built_here

REGISTERED: Final[Mapping[str, ToolSpec]] = {spec.name: spec for spec in TOOL_REGISTRY}

# Model settings that would bind a tool, a server-side tool, or a beta outside
# the registry if a middleware placed them on the request.
FOREIGN_MODEL_SETTINGS: Final[frozenset[str]] = frozenset(
    {"tools", "mcp_servers", "betas", "model_kwargs"}
)

ToolResult = ToolMessage | Command[Any]
ModelResult = ModelResponse[Any] | AIMessage | ExtendedModelResponse[Any]


class ForeignToolError(RuntimeError):
    """Raised before a model call when a tool not built by this package is bound."""


def refusal(name: str, tool_call_id: str) -> ToolMessage:
    """The message the model receives instead of a result when a call is refused."""
    return ToolMessage(
        content=(
            f"blocked: {name!r} is not a tool this system provides. The only tools "
            f"available are {sorted(REGISTERED)}, and none of them sends anything."
        ),
        tool_call_id=tool_call_id,
        status="error",
    )


def is_permitted(name: str) -> bool:
    """True only for a registered name whose capabilities are all allowed."""
    spec = REGISTERED.get(name)
    return spec is not None and spec.capabilities <= ALLOWED_CAPABILITIES


def is_permitted_call(request: ToolCallRequest) -> bool:
    """True only when the tool object, its name, and its spec all check out."""
    name = str(request.tool_call["name"])
    tool = request.tool
    return tool is not None and built_here(tool) and tool.name == name and is_permitted(name)


def foreign_tools(request: ModelRequest[Any]) -> list[str]:
    """Names, or dictionary descriptions, of bound tools this package did not build."""
    names: list[str] = [
        str(getattr(tool, "name", None) or tool)
        for tool in request.tools or []
        if not built_here(tool)
    ]
    return names


class ToolBoundary(AgentMiddleware[Any, Any]):
    """Refuse tool calls and tool bindings that did not come through the constructor."""

    def wrap_tool_call(
        self, request: ToolCallRequest, handler: Callable[[ToolCallRequest], ToolResult]
    ) -> ToolResult:
        """Refuse the call unless its tool is one this package built; otherwise run it."""
        if not is_permitted_call(request):
            return refusal(str(request.tool_call["name"]), str(request.tool_call.get("id") or ""))
        return handler(request)

    async def awrap_tool_call(
        self, request: ToolCallRequest, handler: Callable[[ToolCallRequest], Awaitable[ToolResult]]
    ) -> ToolResult:
        """Asynchronous form of ``wrap_tool_call``, with the same check."""
        if not is_permitted_call(request):
            return refusal(str(request.tool_call["name"]), str(request.tool_call.get("id") or ""))
        return await handler(request)

    def wrap_model_call(
        self,
        request: ModelRequest[Any],
        handler: Callable[[ModelRequest[Any]], ModelResponse[Any]],
    ) -> ModelResult:
        """Stop the run if any tool about to be bound is foreign; otherwise call the model."""
        self._reject_foreign(request)
        return handler(request)

    async def awrap_model_call(
        self,
        request: ModelRequest[Any],
        handler: Callable[[ModelRequest[Any]], Awaitable[ModelResponse[Any]]],
    ) -> ModelResult:
        """Asynchronous form of ``wrap_model_call``, with the same check."""
        self._reject_foreign(request)
        return await handler(request)

    @staticmethod
    def _reject_foreign(request: ModelRequest[Any]) -> None:
        foreign = foreign_tools(request)
        if foreign:
            msg = (
                f"refusing to bind tools this system did not build: {foreign}. Every tool "
                f"must come from blossom.tools.as_langchain_tool."
            )
            raise ForeignToolError(msg)
        settings = FOREIGN_MODEL_SETTINGS & set(request.model_settings or {})
        if settings:
            msg = (
                f"refusing model settings that would bind tools or servers outside the "
                f"registry: {sorted(settings)}."
            )
            raise ForeignToolError(msg)


tool_boundary = ToolBoundary()


def boundary_middleware() -> ToolBoundary:
    """The boundary instance. ``middleware_stack`` appends it; this is for tests."""
    return tool_boundary


def middleware_stack(*others: AgentMiddleware[Any, Any]) -> list[AgentMiddleware[Any, Any]]:
    """Every middleware an agent should carry, with the boundary last.

    Last is innermost for wrap hooks, so whatever the others do to a request is
    visible to the boundary before a tool runs or a model is called. A second
    boundary among ``others`` is refused rather than reordered, so the position
    cannot be argued about in a diff.
    """
    if any(isinstance(other, ToolBoundary) for other in others):
        msg = "the tool boundary is added by middleware_stack; do not list it yourself"
        raise ValueError(msg)
    return [*others, tool_boundary]
