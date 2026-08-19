from collections.abc import Callable
from dataclasses import dataclass

from blossom.drafts import Draft

ToolCallable = Callable[[dict[str, object]], Draft]


@dataclass(frozen=True)
class ToolSpec:
    name: str
    description: str
    capabilities: frozenset[str]
    call: ToolCallable


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
