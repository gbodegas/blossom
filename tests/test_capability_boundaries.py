"""Allowlist-based guards on what the package is capable of.

Everything the package may import is listed here with a justification, and the
each dependency that can reach the network is confined to the single module it
is allowed to live in. A denylist only enumerates what somebody already
thought of; a tool declaring ``post_to_slack`` or a module importing ``aiohttp``
would pass one. Adding anything means editing this file, so every new capability
goes through review.

The regex scan in ``test_architecture_constraints.py`` reads source text, so it
also catches a dynamic ``__import__`` that the AST walk here treats as an
ordinary call.
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
    "anthropic": "model access; confined to the generation seam, see NETWORK_CAPABLE",
    "blossom": "the package itself",
    "chromadb": "vector store; confined to its client module, see NETWORK_CAPABLE",
    "collections": "standard library containers and ABCs",
    "contextlib": "standard library context managers",
    "dataclasses": "standard library",
    "datetime": "standard library",
    "enum": "standard library",
    "fastapi": "the web framework; receives requests, never initiates them",
    "functools": "standard library",
    "json": "standard library; reads fixture files from disk",
    "langsmith": "hosted tracing client; imported only to force tracing off, see NETWORK_CAPABLE",
    "os": "standard library; reads environment variables only",
    "pathlib": "standard library",
    "pydantic": "validation and view models; no I/O",
    "sqlite3": "standard library; local file and in-memory databases only",
    "threading": "standard library; serializes access to the shared connection",
    "typing": "standard library",
    "uuid": "standard library",
}

# Modules that can open a connection to something outside this machine, mapped
# to the only files permitted to import them. Calling a model necessarily reaches
# the network. The claim is that the ability is confined to one named seam per
# dependency, not available anywhere a future route might reach for it.
NETWORK_CAPABLE: dict[str, frozenset[str]] = {
    "anthropic": frozenset({"anthropic_client.py"}),
    "chromadb": frozenset({"chroma_client.py"}),
    "langsmith": frozenset({"settings.py"}),
}


def imports_by_file() -> dict[str, set[str]]:
    """Map each package file to the set of top-level modules it imports."""
    found: dict[str, set[str]] = {}
    for path in sorted(PACKAGE_ROOT.rglob("*.py")):
        relative = path.relative_to(PACKAGE_ROOT).as_posix()
        modules: set[str] = set()
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                modules.update(alias.name.split(".")[0] for alias in node.names)
            elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
                modules.add(node.module.split(".")[0])
        found[relative] = modules
    return found


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
    )

    with pytest.raises(ValueError, match="post_to_school_portal"):
        validate_capabilities([invented])


def test_validation_accepts_the_shipped_registry() -> None:
    validate_capabilities(TOOL_REGISTRY)
