"""The tool registry, and the allowlist that bounds what a tool may do.

There is no send tool. Everything outbound ends in a draft that a human
transmits by hand, and two things here enforce that. ``ToolCallable`` returns
``Draft`` and nothing else, so the registry cannot hold a tool that reports
having transmitted something; do not widen it to ``Any``, the narrowness is the
guarantee. ``ALLOWED_CAPABILITIES`` is an allowlist, not a blocklist: a blocklist
passes any capability nobody thought to ban, an allowlist passes only what is
listed. Adding a capability means editing this set, which shows up in review.
"""

from collections.abc import Callable, Iterable
from dataclasses import dataclass

from blossom.drafts import Draft

ToolCallable = Callable[[dict[str, object]], Draft]

ALLOWED_CAPABILITIES: frozenset[str] = frozenset(
    {
        # Produces text for a human to read, copy, and send themselves.
        "draft",
    }
)


@dataclass(frozen=True)
class ToolSpec:
    """One tool: its name, what it does, and what it is allowed to do.

    ``capabilities`` is checked against ``ALLOWED_CAPABILITIES`` at import;
    ``call`` returns ``Draft`` only.
    """

    name: str
    description: str
    capabilities: frozenset[str]
    call: ToolCallable


def validate_capabilities(registry: Iterable[ToolSpec]) -> None:
    """Raise if any tool declares a capability outside ``ALLOWED_CAPABILITIES``.

    Runs at import, so a tool with an unlisted capability fails before anything
    can call it, whether or not the tests run.
    """
    for tool in registry:
        unlisted = tool.capabilities - ALLOWED_CAPABILITIES
        if unlisted:
            msg = (
                f"tool {tool.name!r} declares capabilities that are not allowed: "
                f"{sorted(unlisted)}. Adding one requires editing "
                f"ALLOWED_CAPABILITIES in blossom/tools.py."
            )
            raise ValueError(msg)


def create_draft(tool_input: dict[str, object]) -> Draft:
    """Produce a draft for a human to read, copy, and send by hand.

    The only tool in the registry; there is no counterpart that transmits.
    """
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
