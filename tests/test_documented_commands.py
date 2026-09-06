"""Guards the setup instructions in the README and the development guide against drift.

These tests parse the shell commands out of the bash and PowerShell fences in
both documents and check that every module path, extra, and version pin they
name exists in the project. They do not run the commands. Every failure names
the document the command came from.
"""

import importlib
import importlib.util
import pathlib
import re
import tomllib
from dataclasses import dataclass

import pytest

REPO_ROOT = pathlib.Path(__file__).resolve().parent.parent
DOCUMENTS = (REPO_ROOT / "README.md", REPO_ROOT / "docs" / "development.md")
"""Every file whose shell fences a reader might paste from."""

SHELL_FENCE = re.compile(r"```(?:bash|powershell)\n(.*?)```", re.DOTALL)
UVICORN_TARGET = re.compile(r"uvicorn\s+([\w.]+):(\w+)")
PYTHON_MODULE_TARGET = re.compile(r"python\s+-m\s+([\w.]+)")
PINNED_INSTALL = re.compile(r"pip install ([^\n]+)")
REQUIREMENT = re.compile(r"([A-Za-z][\w.-]*)==([\w.]+)")


@dataclass(frozen=True)
class ShellBlock:
    """One fenced block of commands and the document it was read from."""

    document: str
    text: str


def shell_blocks() -> list[ShellBlock]:
    """Every bash and PowerShell fence in the documents, in order, each naming its document."""
    return [
        ShellBlock(document=document.relative_to(REPO_ROOT).as_posix(), text=block)
        for document in DOCUMENTS
        for block in SHELL_FENCE.findall(document.read_text(encoding="utf-8"))
    ]


def documented_shell_text() -> str:
    """The concatenated contents of every shell fence in every document."""
    return "\n".join(block.text for block in shell_blocks())


def test_every_document_carries_at_least_one_shell_command() -> None:
    """Fail loudly if the fences disappear, so the other tests cannot pass vacuously."""
    assert documented_shell_text().strip()
    for document in DOCUMENTS:
        assert SHELL_FENCE.search(document.read_text(encoding="utf-8")), (
            f"{document.name} has no shell fence"
        )


def test_the_readme_links_to_the_documents_it_leans_on() -> None:
    """The README points at the development guide and the architecture doc, and both exist."""
    readme = (REPO_ROOT / "README.md").read_text(encoding="utf-8")
    for target in ("docs/development.md", "docs/architecture.md", "LICENSE"):
        assert f"({target})" in readme, f"README does not link to {target}"
        assert (REPO_ROOT / target).exists(), f"{target} is missing"


def test_documented_uvicorn_targets_are_importable() -> None:
    """Every ``uvicorn module:attribute`` target in the documents must resolve."""
    found = False
    for block in shell_blocks():
        for module_path, attribute in UVICORN_TARGET.findall(block.text):
            found = True
            module = importlib.import_module(module_path)
            assert hasattr(module, attribute), (
                f"{block.document}: {module_path} has no attribute {attribute}"
            )
    assert found, "no document shows how to start the server"


def test_documented_python_module_targets_are_importable() -> None:
    """Every ``python -m module`` target in the documents must exist.

    ``pip`` is exempt only where the same block has already run ``ensurepip``,
    which is what puts it there; a venv made by uv does not carry it.
    """
    for block in shell_blocks():
        bootstrapped = False
        for module_path in PYTHON_MODULE_TARGET.findall(block.text):
            if module_path == "ensurepip":
                bootstrapped = True
            if module_path == "pip" and bootstrapped:
                continue
            assert importlib.util.find_spec(module_path) is not None, (
                f"{block.document}: no module {module_path}"
            )


def test_documented_extras_are_ones_the_project_defines() -> None:
    """A documented ``".[extra]"`` install must correspond to a real optional group.

    Dev dependencies live under PEP 735 ``[dependency-groups]``, which does not
    create an extra, so any extra a document names must appear under
    ``optional-dependencies``.
    """
    documented_extras = {
        (block.document, extra)
        for block in shell_blocks()
        for extra in re.findall(r'"\.\[([\w,-]+)\]"', block.text)
    }
    if not documented_extras:
        pytest.skip("no document shows an extras-based install")
    with (REPO_ROOT / "pyproject.toml").open("rb") as handle:
        pyproject = tomllib.load(handle)
    defined = set(pyproject.get("project", {}).get("optional-dependencies", {}))
    undefined = {(document, extra) for document, extra in documented_extras if extra not in defined}
    assert not undefined, f"extras the project does not define (document, extra): {undefined}"


def declared_pins() -> dict[str, str]:
    """Every ``name==version`` pin in pyproject, from both runtime and dev groups."""
    with (REPO_ROOT / "pyproject.toml").open("rb") as handle:
        pyproject = tomllib.load(handle)
    requirements = list(pyproject["project"]["dependencies"])
    for group in pyproject.get("dependency-groups", {}).values():
        requirements.extend(item for item in group if isinstance(item, str))
    pins: dict[str, str] = {}
    for item in requirements:
        match = REQUIREMENT.fullmatch(item)
        if match is not None:
            pins[match.group(1)] = match.group(2)
    return pins


def test_documented_pip_pins_match_pyproject() -> None:
    """Versions in a documented pip install must match the pins in pyproject.toml.

    The pip fallback lists dev tools explicitly because pip before 25.1 cannot
    install a PEP 735 dependency group, so dev pins are compared too.
    """
    declared = declared_pins()
    documented = {
        (block.document, name): version
        for block in shell_blocks()
        for line in PINNED_INSTALL.findall(block.text)
        for name, version in REQUIREMENT.findall(line)
    }
    if not documented:
        pytest.skip("no document shows a pinned install")

    mismatched = {
        f"{document}: {name}": (version, declared.get(name))
        for (document, name), version in documented.items()
        if declared.get(name) != version
    }

    assert not mismatched, (
        f"documented pins disagree with pyproject.toml (documented, declared): {mismatched}"
    )
