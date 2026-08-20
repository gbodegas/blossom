"""Guards the setup instructions in ``README.md`` against drift.

The original scaffold documented a startup sequence that could not work: it
named ``blossom.seed`` and ``blossom.web.app``, neither of which exists, and it
installed a ``[dev]`` extra that the project never defined. None of that was
caught by CI, because nothing in the test suite read the README.

These tests parse the shell commands out of the README and assert that every
module path they name is real. They are deliberately narrow. They do not check
that the commands succeed, only that the things they refer to exist, which is
exactly the class of error that shipped.
"""

import importlib
import importlib.util
import pathlib
import re
import tomllib

import pytest

REPO_ROOT = pathlib.Path(__file__).resolve().parent.parent
README = REPO_ROOT / "README.md"

BASH_FENCE = re.compile(r"```bash\n(.*?)```", re.DOTALL)
UVICORN_TARGET = re.compile(r"uvicorn\s+([\w.]+):(\w+)")
PYTHON_MODULE_TARGET = re.compile(r"python\s+-m\s+([\w.]+)")
PINNED_INSTALL = re.compile(r"pip install ([^\n]+)")
REQUIREMENT = re.compile(r"([A-Za-z][\w.-]*)==([\w.]+)")


def readme_shell_text() -> str:
    """Return the concatenated contents of every ```bash fence in the README."""
    blocks = BASH_FENCE.findall(README.read_text(encoding="utf-8"))
    return "\n".join(blocks)


def test_readme_documents_at_least_one_shell_command() -> None:
    """Fail loudly if the fences disappear, so the other tests cannot pass vacuously."""
    assert readme_shell_text().strip()


def test_documented_uvicorn_targets_are_importable() -> None:
    """Every ``uvicorn module:attribute`` target in the README must resolve."""
    targets: list[tuple[str, str]] = UVICORN_TARGET.findall(readme_shell_text())
    assert targets, "the README no longer documents how to start the server"
    for module_path, attribute in targets:
        module = importlib.import_module(module_path)
        assert hasattr(module, attribute), f"{module_path} has no attribute {attribute}"


def test_documented_python_module_targets_are_importable() -> None:
    """Every ``python -m module`` target in the README must exist.

    This is the check that would have caught the documented ``blossom.seed``
    step, which never existed in the package.
    """
    for module_path in PYTHON_MODULE_TARGET.findall(readme_shell_text()):
        assert importlib.util.find_spec(module_path) is not None, f"no module {module_path}"


def test_readme_only_documents_extras_the_project_defines() -> None:
    """A documented ``".[extra]"`` install must correspond to a real optional group.

    The scaffold documented ``uv pip install -e ".[dev]"`` while declaring its
    development dependencies under PEP 735 ``[dependency-groups]``, which is a
    different mechanism and does not create an extra of that name.
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
    """The pip fallback hardcodes versions; they must not drift from the real pins.

    The fallback exists because ``uv`` cannot reach PyPI from every network,
    and it lists dev tools explicitly because pip could not install a PEP 735
    dependency group until 25.1. Hardcoding versions in prose is exactly how
    an editor ends up running a different mypy than CI, so the pins are checked
    rather than trusted.
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
