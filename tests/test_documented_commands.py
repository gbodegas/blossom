"""Guards the setup instructions in the README and the development guide against drift.

These tests parse the shell commands out of the bash and PowerShell fences in
both documents and check that every module path, extra, and version pin they
name exists in the project. They do not run the commands.
"""

import importlib
import importlib.util
import pathlib
import re
import tomllib

import pytest

REPO_ROOT = pathlib.Path(__file__).resolve().parent.parent
DOCUMENTS = (REPO_ROOT / "README.md", REPO_ROOT / "docs" / "development.md")
"""Every file whose shell fences a reader might paste from."""

SHELL_FENCE = re.compile(r"```(?:bash|powershell)\n(.*?)```", re.DOTALL)
UVICORN_TARGET = re.compile(r"uvicorn\s+([\w.]+):(\w+)")
PYTHON_MODULE_TARGET = re.compile(r"python\s+-m\s+([\w.]+)")
PINNED_INSTALL = re.compile(r"pip install ([^\n]+)")
REQUIREMENT = re.compile(r"([A-Za-z][\w.-]*)==([\w.]+)")


def shell_blocks() -> list[str]:
    """Every bash and PowerShell fence in the documents, in order."""
    return [
        block
        for document in DOCUMENTS
        for block in SHELL_FENCE.findall(document.read_text(encoding="utf-8"))
    ]


def readme_shell_text() -> str:
    """Return the concatenated contents of every shell fence in the documents."""
    return "\n".join(shell_blocks())


def test_readme_documents_at_least_one_shell_command() -> None:
    """Fail loudly if the fences disappear, so the other tests cannot pass vacuously."""
    assert readme_shell_text().strip()
    for document in DOCUMENTS:
        assert SHELL_FENCE.search(document.read_text(encoding="utf-8")), document.name


def test_the_readme_links_to_the_documents_it_leans_on() -> None:
    """The README points at the development guide and the architecture doc, and both exist."""
    readme = (REPO_ROOT / "README.md").read_text(encoding="utf-8")
    for target in ("docs/development.md", "docs/architecture.md", "LICENSE"):
        assert f"({target})" in readme, f"README does not link to {target}"
        assert (REPO_ROOT / target).exists(), f"{target} is missing"


def test_documented_uvicorn_targets_are_importable() -> None:
    """Every ``uvicorn module:attribute`` target in the README must resolve."""
    targets: list[tuple[str, str]] = UVICORN_TARGET.findall(readme_shell_text())
    assert targets, "the README does not document how to start the server"
    for module_path, attribute in targets:
        module = importlib.import_module(module_path)
        assert hasattr(module, attribute), f"{module_path} has no attribute {attribute}"


def test_documented_python_module_targets_are_importable() -> None:
    """Every ``python -m module`` target in the README must exist.

    ``pip`` is exempt only where the same block has already run ``ensurepip``,
    which is what puts it there; a venv made by uv does not carry it.
    """
    for block in shell_blocks():
        bootstrapped = False
        for module_path in PYTHON_MODULE_TARGET.findall(block):
            if module_path == "ensurepip":
                bootstrapped = True
            if module_path == "pip" and bootstrapped:
                continue
            assert importlib.util.find_spec(module_path) is not None, f"no module {module_path}"


def test_readme_only_documents_extras_the_project_defines() -> None:
    """A documented ``".[extra]"`` install must correspond to a real optional group.

    Dev dependencies live under PEP 735 ``[dependency-groups]``, which does not
    create an extra, so any extra the README names must appear under
    ``optional-dependencies``.
    """
    shell_text = readme_shell_text()
    documented_extras = set(re.findall(r'"\.\[([\w,-]+)\]"', shell_text))
    if not documented_extras:
        pytest.skip("the README does not document an extras-based install")
    with (REPO_ROOT / "pyproject.toml").open("rb") as handle:
        pyproject = tomllib.load(handle)
    defined = set(pyproject.get("project", {}).get("optional-dependencies", {}))
    undefined = documented_extras - defined
    assert not undefined, f"README documents extras the project does not define: {undefined}"


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
    """Versions in the README's pip fallback must match the pins in pyproject.toml.

    The fallback lists dev tools explicitly because pip before 25.1 cannot
    install a PEP 735 dependency group, so dev pins are compared too.
    """
    declared = declared_pins()
    documented = {
        name: version
        for line in PINNED_INSTALL.findall(readme_shell_text())
        for name, version in REQUIREMENT.findall(line)
    }
    if not documented:
        pytest.skip("the README does not document any pinned installs")

    mismatched = {
        name: (version, declared.get(name))
        for name, version in documented.items()
        if declared.get(name) != version
    }

    assert not mismatched, (
        f"README pins disagree with pyproject.toml (documented, declared): {mismatched}"
    )
