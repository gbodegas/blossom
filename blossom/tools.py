"""The tool registry, and the allowlist that bounds what a tool may do.

The project's central safety claim is that there is no send tool: everything
outbound terminates in a draft that a human transmits by hand. Two things
enforce that here.

The first is the type. ``ToolCallable`` returns ``Draft`` and nothing else, so
the registry structurally cannot hold a tool that returns the result of having
transmitted something. Do not widen this to ``Any`` for convenience; the
narrowness is the guarantee.

The second is ``ALLOWED_CAPABILITIES``. This used to be checked in the test
suite against a list of banned strings, which is the wrong shape for a safety
property: a tool declaring some capability nobody thought to ban would have
passed. An allowlist inverts that. Adding a capability requires editing this
set, which is a deliberate act that shows up in review.
"""

from collections.abc import Callable, Iterable
from dataclasses import dataclass

from blossom.drafts import Draft

ToolCallable = Callable[[dict[str, object]], Draft]

ALLOWED_CAPABILITIES: frozenset[str] = frozenset(
    {
        # Produces text for a human to read, copy and send themselves.
        "draft",
    }
)


@dataclass(frozen=True)
class ToolSpec:
    name: str
    description: str
    capabilities: frozenset[str]
    call: ToolCallable


def validate_capabilities(registry: Iterable[ToolSpec]) -> None:
    """Raise if any tool declares a capability outside ``ALLOWED_CAPABILITIES``.

    Called at import time rather than only from a test, so a tool with an
    unlisted capability cannot be reached at runtime even if the suite has not
    been run.
    """
    for tool in registry:
        unlisted = tool.capabilities - ALLOWED_CAPABILITIES
        if unlisted:
            msg = (
                f"tool {tool.name!r} declares capabilities that are not allowed: "
                f"{sorted(unlisted)}. Adding one requires editing "
                f"ALLOWED_CAPABILITIES in blossom/tools.py deliberately."
            )
            raise ValueError(msg)


def create_draft(tool_input: dict[str, object]) -> Draft:
    body = str(tool_input.get("body", ""))
    return Draft(body=body)


TOOL_REGISTRY: tuple[ToolSpec, ...] = (
    ToolSpec(
        name="create_manual_draft",
        description="Creates a draft for a human to copy manually.",
        capabilities=frozenset({"draft"}),
        call=create_draft,
    ),
)

validate_capabilities(TOOL_REGISTRY)
