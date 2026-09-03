"""Docstring coverage checks for the blossom package.

Every module needs a module docstring, and every public class, function and
method needs a docstring. These tests check presence only; they do not judge
content. The placeholder modules must also carry a `Status:` or `Known gap`
paragraph so a reader learns from the file itself, not from a grep, that
nothing calls it.
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
    """Placeholder modules must carry a literal `Status:` or `Known gap` paragraph.

    The marker is a literal phrase, not a vocabulary match, so a docstring's
    phrasing cannot accidentally pass or fail the test.
    """
    admits = ("status:", "known gap")
    for relative in (
        "heuristic_relevance.py",
        "chroma_client.py",
        "stores/support_rules.py",
        "agent/loop.py",
    ):
        path = PACKAGE_ROOT / relative
        docstring = ast.get_docstring(ast.parse(path.read_text(encoding="utf-8"))) or ""
        assert any(phrase in docstring.lower() for phrase in admits), (
            f"{relative} is a placeholder but its docstring does not say so"
        )
