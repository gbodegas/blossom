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
constructors to this module, and every object built here wraps a spec that
passed the allowlist. The objects are also remembered by identity, so the
runtime backstop in ``blossom/agent/boundary.py`` can tell one of ours from a
foreign object that copies a registered name.
"""

from collections.abc import Callable, Iterable
from dataclasses import dataclass

from langchain_core.tools import StructuredTool
from pydantic import BaseModel, ConfigDict, Field

from blossom.drafts import Draft

ToolCallable = Callable[[dict[str, object]], Draft]

ALLOWED_CAPABILITIES: frozenset[str] = frozenset(
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


TOOL_REGISTRY: tuple[ToolSpec, ...] = (
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


# Every framework tool object this module has built, kept so the runtime
# backstop can check a tool by identity rather than by name.
_BUILT_HERE: list[StructuredTool] = []


def as_langchain_tool(spec: ToolSpec) -> StructuredTool:
    """Build the framework's tool object from a spec that passed the allowlist.

    The framework validates arguments against ``spec.args_schema`` and calls
    ``spec.call`` with them. The result is the draft serialized as JSON, which
    is what a tool message can carry; the ``Draft`` return type is still
    enforced on ``spec.call`` itself. The object is remembered so ``built_here``
    can vouch for it later.
    """
    validate_capabilities([spec])

    def run(**arguments: object) -> str:
        return spec.call(dict(arguments)).model_dump_json()

    tool = StructuredTool.from_function(
        func=run,
        name=spec.name,
        description=spec.description,
        args_schema=spec.args_schema,
    )
    _BUILT_HERE.append(tool)
    return tool


def built_here(candidate: object) -> bool:
    """True only for an object this module constructed. Identity, not name."""
    return any(candidate is tool for tool in _BUILT_HERE)


LANGCHAIN_TOOLS: tuple[StructuredTool, ...] = tuple(
    as_langchain_tool(spec) for spec in TOOL_REGISTRY
)


def langchain_tools() -> list[StructuredTool]:
    """The registered tools as framework objects, built once, in registry order."""
    return list(LANGCHAIN_TOOLS)
