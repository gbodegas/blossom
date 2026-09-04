"""Allowlist-based guards on what the package is capable of.

Everything the package may import is listed here with a justification, and
each dependency that can reach the network is confined to the single module it
is allowed to live in. A denylist only enumerates what somebody already
thought of; a tool declaring ``post_to_slack`` or a module importing ``aiohttp``
would pass one. Adding anything means editing this file, so every new capability
goes through review.

Three lists do the work. ``ALLOWED_IMPORTS`` admits packages. ``NETWORK_CAPABLE``
and ``TOOL_CONSTRUCTION`` confine paths to named files. ``CLOSED_PREFIXES``
names paths inside admitted packages that no file may import at all.

The regex scan in ``test_architecture_constraints.py`` reads source text and
bans ``__import__`` and ``importlib``, the dynamic imports the AST walk here
would treat as ordinary calls.
"""

import ast
import pathlib

import pytest

from blossom.tools import (
    ALLOWED_CAPABILITIES,
    TOOL_REGISTRY,
    ToolSpec,
    create_draft,
    validate_capabilities,
)

PACKAGE_ROOT = pathlib.Path(__file__).resolve().parent.parent / "blossom"

# Top-level modules the package may import, each with the reason it is here.
ALLOWED_IMPORTS: dict[str, str] = {
    "aiosqlite": "async SQLite driver for the checkpoint store; local files only, see LOCAL_STORES",
    "anthropic": "model access; confined to the model seam, see NETWORK_CAPABLE",
    "blossom": "the package itself",
    "chromadb": "vector store; confined to its client module, see NETWORK_CAPABLE",
    "collections": "standard library containers and ABCs",
    "contextlib": "standard library context managers",
    "ctypes": "standard library; asks Windows whether a drive letter is a network share",
    "dataclasses": "standard library",
    "datetime": "standard library",
    "enum": "standard library",
    "fastapi": "the web framework; receives requests, never initiates them",
    "functools": "standard library",
    "httpx": "the HTTP client under the SDK; confined to the model seam, see NETWORK_CAPABLE",
    "json": "standard library; reads fixture files from disk",
    "langchain": "agent middleware; the tool backstop is confined, see TOOL_CONSTRUCTION",
    "langchain_anthropic": "the model client; confined to the model seam, see NETWORK_CAPABLE",
    "langchain_core": (
        "tool and message types; construction is confined, see TOOL_CONSTRUCTION, and "
        "the hosted tracer is closed off, see CLOSED_PREFIXES"
    ),
    "langgraph": (
        "graph, interrupts, and checkpoints; its remote-server client is closed off, "
        "see CLOSED_PREFIXES"
    ),
    "langsmith": "hosted tracing client; imported only to force tracing off, see NETWORK_CAPABLE",
    "os": "standard library; reads environment variables only",
    "pathlib": "standard library",
    "pydantic": "validation and view models; no I/O",
    "sqlite3": "standard library; local file and in-memory databases only",
    "threading": "standard library; serializes access to the shared connection",
    "typing": "standard library",
    "uuid": "standard library",
    "zoneinfo": "standard library; resolves the household zone, backed by tzdata",
}

# Modules that can open a connection to something outside this machine, mapped
# to the only files permitted to import them. Calling a model necessarily reaches
# the network. The claim is that the ability is confined to one named seam per
# dependency, not available anywhere a future route might reach for it.
NETWORK_CAPABLE: dict[str, frozenset[str]] = {
    "anthropic": frozenset({"anthropic_client.py"}),
    "chromadb": frozenset({"chroma_client.py"}),
    "httpx": frozenset({"anthropic_client.py"}),
    "langchain_anthropic": frozenset({"anthropic_client.py"}),
    "langsmith": frozenset({"settings.py"}),
}

# Storage drivers and the checkpoint saver, mapped to the one module that may
# open them, so a connection to the checkpoint file cannot be opened by a route
# or a graph node with different pragmas or a permissive serializer.
LOCAL_STORES: dict[str, frozenset[str]] = {
    "aiosqlite": frozenset({"stores/checkpoints.py"}),
    "langgraph.checkpoint.sqlite": frozenset({"stores/checkpoints.py"}),
}

# Import paths no file in the package may use, each with the reason. They sit
# inside packages the allowlist admits, so the top-level check cannot see them.
# Matching is by prefix, as for TOOL_CONSTRUCTION, and a whole subpackage is
# closed where a narrower path could be reached through a re-export.
CLOSED_PREFIXES: dict[str, str] = {
    "langgraph.pregel": (
        "the runtime's internals, including the client that runs a graph on a "
        "remote server through langgraph_sdk and langsmith"
    ),
    "langgraph_sdk": "the client for a hosted graph server",
    "langchain_core.tracers": (
        "the hosted tracer, its lazy re-export at the package level, and the "
        "context manager that turns hosted tracing on with no environment variable set"
    ),
    "langchain.chat_models": (
        "builds a provider client from the environment; the seam is anthropic_client.py"
    ),
    "langchain.embeddings": "builds a provider client from the environment",
}

# The framework's ways of bringing a tool into existence, wiring tools into a
# graph or an agent, or intercepting a tool call, mapped to the only files
# permitted to import them. A framework tool built anywhere but tools.py would
# skip the registry; an agent or tool node built elsewhere could run without
# the backstop; a second middleware could reorder or shadow it. Keys match
# dotted module paths by prefix and the longest matching key decides, so
# ``langchain.agents.middleware`` is governed by its own entry and not by
# ``langchain.agents``. An ``import x.y`` and a ``from x import y`` both count
# as the dotted name ``x.y``.
TOOL_CONSTRUCTION: dict[str, frozenset[str]] = {
    "langchain_core.tools": frozenset({"tools.py"}),
    "langchain.tools": frozenset({"tools.py"}),
    "langchain.agents": frozenset({"agent/graph.py"}),
    "langchain.agents.middleware": frozenset({"agent/boundary.py"}),
    "langgraph.prebuilt": frozenset({"agent/graph.py"}),
}


def imports_by_file() -> dict[str, set[str]]:
    """Map each package file to the set of top-level modules it imports."""
    return {
        relative: {name.split(".")[0] for name in dotted}
        for relative, dotted in dotted_imports_by_file().items()
    }


def dotted_imports(source: str) -> set[str]:
    """The full dotted paths one source text imports.

    ``import x.y`` records ``x.y``; ``from x import y`` records both ``x`` and
    ``x.y``, so a name reached through a package re-export is seen under the
    package as well as under the name.
    """
    modules: set[str] = set()
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, ast.Import):
            modules.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
            modules.add(node.module)
            modules.update(f"{node.module}.{alias.name}" for alias in node.names)
    return modules


def dotted_imports_by_file() -> dict[str, set[str]]:
    """Map each package file to the full dotted module paths it imports."""
    return {
        path.relative_to(PACKAGE_ROOT).as_posix(): dotted_imports(path.read_text(encoding="utf-8"))
        for path in sorted(PACKAGE_ROOT.rglob("*.py"))
    }


def closed_hits(dotted: set[str]) -> set[str]:
    """The imported names that fall under a closed prefix."""
    return {name for name in dotted for prefix in CLOSED_PREFIXES if matches(prefix, name)}


def matches(prefix: str, dotted: str) -> bool:
    """True when ``dotted`` is ``prefix`` itself or a name beneath it."""
    return dotted == prefix or dotted.startswith(prefix + ".")


def test_the_prefix_matcher_does_not_match_by_accident() -> None:
    assert matches("langchain.tools", "langchain.tools")
    assert matches("langchain.tools", "langchain.tools.StructuredTool")
    assert not matches("langchain.tools", "langchain.toolsets")
    assert not matches("langchain.tools", "langchain")


def governing_prefix(dotted: str) -> str | None:
    """The longest TOOL_CONSTRUCTION key that covers ``dotted``, if any."""
    covering = [prefix for prefix in TOOL_CONSTRUCTION if matches(prefix, dotted)]
    return max(covering, key=len) if covering else None


def test_the_longest_matching_prefix_governs() -> None:
    middleware = "langchain.agents.middleware"
    assert governing_prefix(f"{middleware}.AgentMiddleware") == middleware
    assert governing_prefix("langchain.agents.create_agent") == "langchain.agents"
    assert governing_prefix("langchain.agents") == "langchain.agents"
    assert governing_prefix("langchain.chat_models") is None


def test_tool_construction_is_confined_to_its_seam() -> None:
    """Only tools.py may build framework tools; only boundary.py may intercept calls."""
    violations: dict[str, set[str]] = {}
    for relative, dotted in dotted_imports_by_file().items():
        for name in dotted:
            prefix = governing_prefix(name)
            if prefix is not None and relative not in TOOL_CONSTRUCTION[prefix]:
                violations.setdefault(relative, set()).add(name)

    assert not violations, f"tool construction outside its seam: {violations}"


def test_the_seam_scan_sees_the_imports_it_is_meant_to_confine() -> None:
    """Positive control: the scan must see the permitted imports where they live.

    Without this, a scan that saw nothing would pass the confinement test.
    """
    dotted = dotted_imports_by_file()

    assert "langchain_core.tools.StructuredTool" in dotted["tools.py"]
    assert "langchain.agents.middleware.AgentMiddleware" in dotted["agent/boundary.py"]
    assert "langchain_anthropic.ChatAnthropic" in dotted["anthropic_client.py"]


def test_local_stores_are_opened_only_by_the_checkpoint_module() -> None:
    """The driver and the saver are imported where the pragmas and serializer are set."""
    violations: dict[str, set[str]] = {}
    for relative, dotted in dotted_imports_by_file().items():
        for prefix, permitted in LOCAL_STORES.items():
            hits = {name for name in dotted if matches(prefix, name)}
            if hits and relative not in permitted:
                violations.setdefault(relative, set()).update(hits)

    assert not violations, f"store drivers opened outside the checkpoint module: {violations}"
    assert "aiosqlite" in dotted_imports_by_file()["stores/checkpoints.py"]


def test_closed_prefixes_are_imported_nowhere() -> None:
    """Paths inside admitted packages that no file may import, whatever the reason."""
    violations = {
        relative: hits
        for relative, dotted in dotted_imports_by_file().items()
        if (hits := closed_hits(dotted))
    }

    assert not violations, f"closed import paths in use: {violations}"


@pytest.mark.parametrize(
    "snippet",
    [
        "from langchain_core.tracers.langchain import LangChainTracer",
        "from langchain_core.tracers import LangChainTracer",
        "from langchain_core import tracers",
        "import langchain_core.tracers.context",
        "from langchain_core.tracers.context import tracing_v2_enabled",
        "import langgraph.pregel.remote as remote",
        "from langgraph.pregel import remote",
        "from langgraph import pregel",
        "from langgraph_sdk import get_client",
        "from langchain.chat_models import init_chat_model",
        "def f():\n    from langgraph_sdk import get_client\n    return get_client",
    ],
)
def test_the_closed_prefix_scan_flags_each_spelling(snippet: str) -> None:
    """Positive control: every way of reaching a closed path is seen by the scan."""
    assert closed_hits(dotted_imports(snippet))


def test_the_closed_prefix_scan_leaves_the_permitted_paths_alone() -> None:
    assert not closed_hits(dotted_imports("from langgraph.graph import StateGraph"))
    assert not closed_hits(dotted_imports("from langchain_anthropic import ChatAnthropic"))


def test_the_package_has_files_to_scan() -> None:
    """Guard against the scan silently covering nothing."""
    assert len(imports_by_file()) > 10


def test_every_imported_module_is_on_the_allowlist() -> None:
    """A new third-party import must be justified in ALLOWED_IMPORTS first."""
    unlisted: dict[str, set[str]] = {}
    for relative, modules in imports_by_file().items():
        extra = modules - ALLOWED_IMPORTS.keys()
        if extra:
            unlisted[relative] = extra

    assert not unlisted, (
        f"unlisted imports: {unlisted}. Add each to ALLOWED_IMPORTS with the reason "
        f"it is safe, and to NETWORK_CAPABLE if it can reach off this machine."
    )


def test_network_capable_modules_are_confined_to_their_seam() -> None:
    """Each module in NETWORK_CAPABLE may only be imported where the design says it lives."""
    violations: dict[str, set[str]] = {}
    for relative, modules in imports_by_file().items():
        for module, permitted in NETWORK_CAPABLE.items():
            if module in modules and relative not in permitted:
                violations.setdefault(relative, set()).add(module)

    assert not violations, f"network-capable imports outside their seam: {violations}"


def test_every_registered_tool_declares_only_allowed_capabilities() -> None:
    for tool in TOOL_REGISTRY:
        assert tool.capabilities <= ALLOWED_CAPABILITIES


def test_the_allowlist_permits_drafting_and_nothing_else() -> None:
    """Drafting is the only capability any tool may declare."""
    assert ALLOWED_CAPABILITIES == frozenset({"draft"})


def test_registry_validation_rejects_a_capability_nobody_thought_to_ban() -> None:
    """An invented capability not on the allowlist is rejected by name."""
    invented = ToolSpec(
        name="post_to_school_portal",
        description="Anything a denylist did not anticipate.",
        capabilities=frozenset({"post_to_school_portal"}),
        call=create_draft,
        args_schema=TOOL_REGISTRY[0].args_schema,
    )

    with pytest.raises(ValueError, match="post_to_school_portal"):
        validate_capabilities([invented])


def test_validation_accepts_the_shipped_registry() -> None:
    validate_capabilities(TOOL_REGISTRY)
