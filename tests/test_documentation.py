"""Keeps the package's documentation from drifting back apart.

Before this test, 18 of 20 modules had no module docstring and 69 public
symbols had none. The split was not random: every module written or revised
recently was documented thoroughly, and every module inherited from the
original scaffold had nothing at all. That is worse than uniformly sparse
documentation, because the asymmetry silently implies the documented modules
are the ones that matter.

These tests are deliberately about presence, not quality. No test can judge
whether a docstring is any good. What a test can do is make the absence of one
a visible failure at the moment it is introduced, rather than a slow drift
nobody notices until the next audit.
"""

import ast
import pathlib

import pytest

PACKAGE_ROOT = pathlib.Path(__file__).resolve().parent.parent / "blossom"


def python_files() -> list[pathlib.Path]:
    """Every module in the package, excluding empty package markers."""
    return [
        path
        for path in sorted(PACKAGE_ROOT.rglob("*.py"))
        if not (path.name == "__init__.py" and not path.read_text(encoding="utf-8").strip())
    ]


def documented_symbols(tree: ast.Module) -> list[tuple[str, int, bool]]:
    """Return (qualified name, line, has docstring) for every public top-level symbol."""
    found: list[tuple[str, int, bool]] = []
    for node in tree.body:
        if not isinstance(node, ast.ClassDef | ast.FunctionDef):
            continue
        if node.name.startswith("_"):
            continue
        found.append((node.name, node.lineno, ast.get_docstring(node) is not None))
        if isinstance(node, ast.ClassDef):
            for member in node.body:
                if isinstance(member, ast.FunctionDef) and not member.name.startswith("_"):
                    found.append(
                        (
                            f"{node.name}.{member.name}",
                            member.lineno,
                            ast.get_docstring(member) is not None,
                        )
                    )
    return found


def test_the_scan_covers_the_whole_package() -> None:
    """Guard against the other tests passing because they found nothing to check."""
    assert len(python_files()) >= 20


@pytest.mark.parametrize("path", python_files(), ids=lambda p: p.name)
def test_every_module_has_a_module_docstring(path: pathlib.Path) -> None:
    tree = ast.parse(path.read_text(encoding="utf-8"))

    assert ast.get_docstring(tree) is not None, (
        f"{path.relative_to(PACKAGE_ROOT.parent)} has no module docstring. Say what the "
        f"module is for and, if it is a placeholder, say that too."
    )


@pytest.mark.parametrize("path", python_files(), ids=lambda p: p.name)
def test_every_public_symbol_has_a_docstring(path: pathlib.Path) -> None:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    undocumented = [
        f"{name} (line {line})" for name, line, has_doc in documented_symbols(tree) if not has_doc
    ]

    assert not undocumented, (
        f"{path.relative_to(PACKAGE_ROOT.parent)} has undocumented public symbols: "
        f"{', '.join(undocumented)}"
    )


def test_modules_that_are_placeholders_say_so() -> None:
    """Several modules are seams or stubs. Each must admit it in its own docstring.

    These are the files most likely to be mistaken for working code: they are
    syntactically complete and type-check cleanly. A reader who opens
    `heuristic_relevance.py` should learn from the module itself that nothing
    calls it, rather than from a grep.

    The convention enforced here is a literal `Status:` or `Known gap`
    paragraph, rather than a list of words that might mean "unfinished". A
    vocabulary guess makes the test both weak and surprising -- it passed a
    docstring saying "does not work yet" only by accident of phrasing, and
    failed another that was equally honest.
    """
    admits = ("status:", "known gap")
    for relative in (
        "heuristic_relevance.py",
        "chroma_client.py",
        "anthropic_client.py",
        "stores/support_rules.py",
        "agent/loop.py",
    ):
        path = PACKAGE_ROOT / relative
        docstring = ast.get_docstring(ast.parse(path.read_text(encoding="utf-8"))) or ""
        assert any(phrase in docstring.lower() for phrase in admits), (
            f"{relative} is a placeholder but its docstring does not say so"
        )
