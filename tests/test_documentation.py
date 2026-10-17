# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Gerardo Bodegas Martinez
"""Docstring coverage checks for the blossom package.

Every module needs a module docstring, and every public class, function and
method needs a docstring. These tests check presence only; they do not judge
content.
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


GUIDE = pathlib.Path(__file__).resolve().parent.parent / "docs" / "development.md"
"""The household guide, in the repository."""

WHAT_A_REVIEW_KEEPS = (
    "In grade records the student and teacher lines are never kept as text; only the keyed form "
    "of her name is. While a screenshot review is open, Blossom keeps the student name it read "
    "from the screenshots, as text, with the screenshots, and removes it with them when the "
    "review finishes, is discarded or expires. That removal can wait for the next hourly "
    "cleanup, or the next start. A teacher's name is never kept.",
    "A review's screenshots, what Blossom read from them, and any progress saved for later are "
    "removed from Blossom's database file when the review finishes, is discarded, or expires.",
    "This removes them from the file; it doesn't guarantee they are erased from the disk, and a "
    "backup or a disk snapshot taken while the review was open may still hold earlier copies.",
    "the first grade report or the first homework screenshot review makes `household.secret`",
)
"""What the guide says a screenshot review keeps and removes, and when the household secret is
made, word for word."""


def test_the_household_guide_says_what_a_screenshot_review_keeps() -> None:
    text = " ".join(GUIDE.read_text(encoding="utf-8").split())
    planner = text.split("## What the planner shares", 1)[1].split("## ", 1)[0]

    assert [line for line in WHAT_A_REVIEW_KEEPS if line not in text] == []
    assert "The student and teacher lines are never kept as text" not in text
    assert WHAT_A_REVIEW_KEEPS[1] in planner
    assert WHAT_A_REVIEW_KEEPS[2] in planner
