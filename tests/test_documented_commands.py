# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Gerardo Bodegas Martinez
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


HEADING = re.compile(r"^(#{1,6}) +(.+?)(?: +#+)? *$", re.MULTILINE)
"""A heading's level and text, without the closing run of #s Markdown allows."""
FENCE = re.compile(r"^(`{3,}|~{3,}).*?^\1", re.MULTILINE | re.DOTALL)
GUIDE_LINK = re.compile(r"\]\(docs/development\.md#([^)\s]+)\)")
MARKUP = re.compile(r"[*\[\]<>]|(?<!\w)_|_(?!\w)")
"""Emphasis, a link or HTML in a heading, which GitHub names by the text they show."""


def anchor_of(heading: str) -> str:
    """The anchor GitHub gives a heading before it numbers repeats: lowercase, punctuation
    and code marks dropped, spaces as hyphens. A heading with markup outside its code is
    refused."""
    if MARKUP.search(re.sub(r"`[^`]*`", "", heading)):
        msg = f"the heading {heading!r} holds markup"
        raise AssertionError(msg)
    return re.sub(r"[^\w\- ]", "", heading.lower()).replace(" ", "-")


def section_of(document: str, anchor: str) -> str:
    """The prose under the heading ``anchor`` names, up to the next heading at its level or
    above. Fences of either kind are left out first, so a comment in a command block is no
    heading, and a repeated anchor takes -1, -2 and on, as GitHub numbers them."""
    prose = FENCE.sub("", document)
    headings = list(HEADING.finditer(prose))
    seen: dict[str, int] = {}
    for index, heading in enumerate(headings):
        named = given = anchor_of(heading.group(2))
        while given in seen:
            seen[named] += 1
            given = f"{named}-{seen[named]}"
        seen[given] = 0
        if given == anchor:
            level = len(heading.group(1))
            ends = [one.start() for one in headings[index + 1 :] if len(one.group(1)) <= level]
            return prose[heading.end() : ends[0] if ends else len(prose)]
    msg = f"no heading in the guide has the anchor #{anchor}"
    raise AssertionError(msg)


FALLBACK_SENTENCE = (
    "If there's no `py` launcher, check that `python --version` says 3.12 or 3.13 and use "
    "`python -m venv .venv` in its place."
)


def check_install_fallback(readme: str, guide: str) -> None:
    """Assert that right after the README's install commands, a note sends a reader without
    Windows' ``py`` launcher to a section of the guide that gives `FALLBACK_SENTENCE` word
    for word."""
    install = readme.split("**Install once.**", 1)[1].split("**Start the sample.**", 1)[0]
    commands, note = install.rsplit("```", 1)
    assert "py -3.12 -m venv .venv" in commands
    assert "`py` launcher" in note, "the note after the install commands skips a missing `py`"
    anchors = GUIDE_LINK.findall(note)
    assert anchors, "the note after the install commands links to no section of the guide"
    for anchor in anchors:
        section = " ".join(section_of(guide, anchor).split())
        assert FALLBACK_SENTENCE in section, f"#{anchor} doesn't give the fallback"


def test_the_readme_install_has_a_way_on_without_the_py_launcher() -> None:
    readme = (REPO_ROOT / "README.md").read_text(encoding="utf-8")
    guide = (REPO_ROOT / "docs" / "development.md").read_text(encoding="utf-8")
    check_install_fallback(readme, guide)


FALLBACK = (
    "If there's no `py` launcher, check that `python --version` says 3.12 or 3.13 and use\n"
    "`python -m venv .venv` in its place.\n"
)


def readme_linking_to(anchor: str) -> str:
    return (
        "**Install once.**\n\n```powershell\npy -3.12 -m venv .venv\n```\n\n"
        f"If there's no `py` launcher, use [the fallback](docs/development.md#{anchor}).\n\n"
        "**Start the sample.**\n"
    )


MISSES_THE_FALLBACK = {
    "no-such-heading": ("## Fallback\n\n" + FALLBACK, "fallbacks"),
    "another-section": ("## Fallback\n\n" + FALLBACK + "\n## With uv\n\nRun uv.\n", "with-uv"),
    "a-repeated-heading": (
        "## Fallback\n\nNone.\n\n## Fallback\n\nNone.\n\n## Fallback 1\n\n" + FALLBACK,
        "fallback-1",
    ),
    "markup-in-the-heading": (
        "## Other ways to start the _sample_\n\n" + FALLBACK,
        "other-ways-to-start-the-_sample_",
    ),
    "taken-apart": (
        "## Fallback\n\nCheck that `python --version` says 3.12 or 3.13. Never use "
        "`python -m venv .venv` without the `py` launcher.\n",
        "fallback",
    ),
    "told-not-to": (
        "## Fallback\n\nIf there's no `py` launcher, don't check that `python --version` "
        "says 3.12 or 3.13 and never use `python -m venv .venv` in its place.\n",
        "fallback",
    ),
}
LANDS_ON_THE_FALLBACK = {
    "a-comment-in-a-tilde-fence": (
        "## Fallback\n\nRun:\n\n~~~bash\n# create it\npy -3.12 -m venv .venv\n~~~\n\n" + FALLBACK,
        "fallback",
    ),
    "a-comment-in-a-backtick-fence": (
        "## Fallback\n\n```bash\n# create it\n```\n\n" + FALLBACK,
        "fallback",
    ),
    "a-repeat-numbered": ("## Fallback\n\nNone.\n\n## Fallback\n\n" + FALLBACK, "fallback-1"),
    "under-a-subsection": (
        "## Fallback\n\nFirst this.\n\n### Detail\n\n" + FALLBACK + "\n## With uv\n",
        "fallback",
    ),
    "a-closing-hash-run": ("## Fallback ##\n\n" + FALLBACK, "fallback"),
    "an-underscore-in-code": ("## The `_private` fallback\n\n" + FALLBACK, "the-_private-fallback"),
    "code-in-the-heading": ("## The `py` fallback\n\n" + FALLBACK, "the-py-fallback"),
    "an-underscore-in-a-name": (
        "## BLOSSOM_TODAY and Python\n\n" + FALLBACK,
        "blossom_today-and-python",
    ),
}


@pytest.mark.parametrize(
    ("guide", "anchor"), MISSES_THE_FALLBACK.values(), ids=MISSES_THE_FALLBACK.keys()
)
def test_a_link_that_misses_the_fallback_fails(guide: str, anchor: str) -> None:
    with pytest.raises(AssertionError):
        check_install_fallback(readme_linking_to(anchor), guide)


@pytest.mark.parametrize(
    ("guide", "anchor"), LANDS_ON_THE_FALLBACK.values(), ids=LANDS_ON_THE_FALLBACK.keys()
)
def test_a_link_that_lands_on_the_fallback_passes(guide: str, anchor: str) -> None:
    check_install_fallback(readme_linking_to(anchor), guide)


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
