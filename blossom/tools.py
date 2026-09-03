"""The tool registry, the allowlist that bounds what a tool may do, and the one
place a framework tool object is constructed.

There is no send tool. Everything outbound ends in a draft that a human
transmits by hand, and three things here enforce that. ``ToolCallable`` returns
``Draft`` and nothing else, so the registry cannot hold a tool that reports
having transmitted something; do not widen it to ``Any``, the narrowness is the
guarantee. ``ALLOWED_CAPABILITIES`` is an allowlist, not a blocklist: a blocklist
passes any capability nobody thought to ban, an allowlist passes only what is
listed. Adding a capability means editing this set, which shows up in review.
And ``as_langchain_tool`` is the only constructor of framework tool objects
this package provides: a test confines the framework's direct tool
constructors to this module, and the constructor builds only for an entry of
``TOOL_REGISTRY``, checked by identity, so a spec assembled anywhere else is
refused whatever capability it claims. The objects are remembered by identity
too, so the runtime backstop in ``blossom/agent/boundary.py`` can tell one of
ours from a foreign object that copies a registered name.

What a registered callable does before it returns is outside the reach of any
check here. It is bounded by review of this file, where every entry is visible,
and by the import scans, which keep the named network-capable dependencies in
their own seams and ban the transmitting calls somebody already thought of.
Neither is a sandbox against the author of this file. The one thing enforced at
runtime is that the callable returned a ``Draft``.

The identity registry is a record this package keeps for itself. It tells this
module's objects from foreign ones; it does not defend against code inside this
package that edits it, which is what review is for. The names it rests on are
declared ``Final`` so a rebinding at least fails type checking.
"""

from collections.abc import Callable, Iterable
from dataclasses import dataclass
from typing import Final

from langchain_core.tools import StructuredTool
from pydantic import BaseModel, ConfigDict, Field

from blossom.drafts import Draft

ToolCallable = Callable[[dict[str, object]], Draft]

ALLOWED_CAPABILITIES: Final[frozenset[str]] = frozenset(
    {
        # Produces text for a human to read, copy, and send themselves.
        "draft",
    }
)


class DraftRequest(BaseModel):
    """Arguments for the draft tool. The description is what the model reads."""

    model_config = ConfigDict(extra="forbid")

    body: str = Field(description="The text a person will read, copy, and send themselves.")


@dataclass(frozen=True)
class ToolSpec:
    """One tool: its name, what it does, what it accepts, and what it may do.

    ``capabilities`` is checked against ``ALLOWED_CAPABILITIES`` at import;
    ``call`` returns ``Draft`` only. ``args_schema`` is the argument model the
    framework validates against before ``call`` runs; its field descriptions are
    part of the contract the model reads, so they are written as a spec.
    """

    name: str
    description: str
    capabilities: frozenset[str]
    call: ToolCallable
    args_schema: type[BaseModel]


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


TOOL_REGISTRY: Final[tuple[ToolSpec, ...]] = (
    ToolSpec(
        name="create_manual_draft",
        description=(
            "Write a draft message for a person in the family to read and, if "
            "they choose, copy and send themselves. Nothing is sent by this tool "
            "or any other. Use it for anything that would leave the family: a "
            "note to a teacher, an extension request, a registration reminder."
        ),
        capabilities=frozenset({"draft"}),
        call=create_draft,
        args_schema=DraftRequest,
    ),
)

validate_capabilities(TOOL_REGISTRY)


# Every framework tool object this module has built, paired with the function
# it was built around, so the runtime backstop can check a tool by identity and
# confirm that what it would run is still what was built.
_BUILT_HERE: Final[list[tuple[StructuredTool, Callable[..., str]]]] = []


def serialize_draft(name: str, result: object) -> str:
    """Serialize a tool's result for a tool message; refuse anything but a ``Draft``.

    ``ToolCallable`` is a static promise. This is the runtime check behind it,
    so a callable that returns something other than a ``Draft`` fails here
    instead of reaching the model as a tool result. It runs after the callable
    has returned, so it bounds what the model sees, not what the callable did.
    """
    if not isinstance(result, Draft):
        msg = f"tool {name!r} returned {type(result).__name__}, not Draft"
        raise TypeError(msg)
    return result.model_dump_json()


def as_langchain_tool(spec: ToolSpec) -> StructuredTool:
    """Build the framework's tool object for an entry of ``TOOL_REGISTRY``.

    Membership is checked by identity, not equality, so an equal copy of a
    registry entry is refused along with anything invented; an allowed
    capability on its own is not enough. The callable is captured at build
    time, so a later change to the spec does not reach a tool already built.
    The framework validates arguments against ``spec.args_schema`` and calls
    the captured callable; the result passes through ``serialize_draft``. The
    object and its function are remembered so ``built_here`` can vouch for
    them later.
    """
    if not any(spec is entry for entry in TOOL_REGISTRY):
        msg = (
            f"tool {spec.name!r} is not an entry of TOOL_REGISTRY. Only registry "
            f"entries become framework tools, and adding one means editing "
            f"blossom/tools.py."
        )
        raise ValueError(msg)
    call = spec.call

    def run(**arguments: object) -> str:
        return serialize_draft(spec.name, call(dict(arguments)))

    tool = StructuredTool.from_function(
        func=run,
        name=spec.name,
        description=spec.description,
        args_schema=spec.args_schema,
    )
    _BUILT_HERE.append((tool, run))
    return tool


def built_here(candidate: object) -> bool:
    """True only for an object this module built that still carries its original function.

    Identity, not name: a lookalike is refused. Identity of the function too,
    so a built object whose function was swapped is not vouched for.
    """
    return any(candidate is tool and tool.func is run for tool, run in _BUILT_HERE)


LANGCHAIN_TOOLS: Final[tuple[StructuredTool, ...]] = tuple(
    as_langchain_tool(spec) for spec in TOOL_REGISTRY
)


def langchain_tools() -> list[StructuredTool]:
    """The registered tools as framework objects, built once, in registry order."""
    return list(LANGCHAIN_TOOLS)
