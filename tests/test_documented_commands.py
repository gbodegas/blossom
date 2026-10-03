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


HEADING = re.compile(r"^(#{1,6}) +(.+?)(?: +#+)? *\r?$", re.MULTILINE)
"""A heading's level and text, without the closing run of #s Markdown allows."""
FENCE = re.compile(
    r"^(?:(?P<shallow> {0,3})|(?P<deep>[ \t]*))(?P<run>(?P<mark>[`~])(?P=mark){2,})"
    r"(?!(?<=`)[^\n]*`)[^\n]*"
    r"(?:\n(?:[^\n]*\n)*?(?(shallow) {0,3}|(?P=deep))(?P=run)(?P=mark)*[ \t]*\r?$|.*\Z)",
    re.MULTILINE | re.DOTALL,
)
"""A fenced code block as GitHub reads it: a run of three or more backticks or tildes at any
depth, so a step in a list counts, closed by a bare run of the same mark at least as long, at
most three spaces in when the opener is, or as deep as a deeper opener, or by the end of the
document when nothing closes it."""
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
    "in-a-fence-left-open": ("```markdown\n## Fallback\n\n" + FALLBACK_SENTENCE + "\n", "fallback"),
    "in-a-tilde-fence-left-open": ("~~~\n## Fallback\n\n" + FALLBACK, "fallback"),
    "after-a-shorter-run": ("````\nx\n```\n## Fallback\n\n" + FALLBACK, "fallback"),
    "after-the-other-mark": ("```\nx\n~~~\n## Fallback\n\n" + FALLBACK, "fallback"),
    "after-a-run-with-words": ("```markdown\nx\n```bash\n## Fallback\n\n" + FALLBACK, "fallback"),
    "in-a-fence-three-spaces-in": ("   ```\n## Fallback\n\n" + FALLBACK, "fallback"),
    "after-a-run-four-spaces-in": ("```\nx\n    ```\n## Fallback\n\n" + FALLBACK, "fallback"),
    "after-a-run-past-a-tab": ("```\nx\n\t```\n## Fallback\n\n" + FALLBACK, "fallback"),
    "after-a-run-deeper-than-a-shallow-opener": (
        " ```bash\n x\n    ```\n## Fallback\n\n" + FALLBACK,
        "fallback",
    ),
    "after-a-run-shallower-than-a-list-step": (
        "1. Run:\n\n    ```bash\n    x\n  ```\n## Fallback\n\n" + FALLBACK,
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
    "a-heading-ending-in-crlf": ("## Fallback ##\r\n\r\n" + FALLBACK, "fallback"),
    "an-underscore-in-code": ("## The `_private` fallback\n\n" + FALLBACK, "the-_private-fallback"),
    "code-in-the-heading": ("## The `py` fallback\n\n" + FALLBACK, "the-py-fallback"),
    "an-underscore-in-a-name": (
        "## BLOSSOM_TODAY and Python\n\n" + FALLBACK,
        "blossom_today-and-python",
    ),
    "after-a-closed-fence": (
        "```markdown\n## Fallback\n```\n\n## Fallback\n\n" + FALLBACK,
        "fallback",
    ),
    "after-a-longer-run": ("```\n## Fallback\n`````\n\n## Fallback\n\n" + FALLBACK, "fallback"),
    "after-spaces-past-the-run": ("```\nx\n```  \n\n## Fallback\n\n" + FALLBACK, "fallback"),
    "after-an-empty-fence": ("```\n```\n\n## Fallback\n\n" + FALLBACK, "fallback"),
    "after-a-fence-closed-with-crlf": (
        "## Fallback\r\n\r\n```bash\r\n# create it\r\n```\r\n\r\n" + FALLBACK,
        "fallback",
    ),
    "after-a-run-three-spaces-in": ("```\nx\n   ```\n\n## Fallback\n\n" + FALLBACK, "fallback"),
    "after-a-fence-in-a-list-step": (
        "1. Run:\n\n    ```bash\n    # x\n    ```\n\n## Fallback\n\n" + FALLBACK,
        "fallback",
    ),
    "two-backticks-are-no-fence": ("``\n\n## Fallback\n\n" + FALLBACK, "fallback"),
    "a-backtick-in-its-words-is-no-fence": ("```a`b\n\n## Fallback\n\n" + FALLBACK, "fallback"),
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


COMMENT = re.compile(r"(?:^|(?<=\s))#.*$", re.MULTILINE)
"""A shell comment: a ``#`` that opens a line or follows a space, to the end of its line."""


POWERSHELL = frozenset(("powershell", "pwsh", "ps1"))
"""The names a fence gives PowerShell, the shell the household steps run in."""
SINGLE = "'" + "".join(map(chr, range(0x2018, 0x201C)))
DOUBLE = '"' + "".join(map(chr, range(0x201C, 0x201F)))
STRING = re.compile(
    r"@(['\"])[ \t]*\r?\n.*?(?:\n\1@|\Z)|<#.*?(?:#>|\Z)|#[^\n]*"
    rf"|[{SINGLE}](?:[^{SINGLE}]|[{SINGLE}]{{2}})*(?:[{SINGLE}]|\Z)"
    rf"|[{DOUBLE}](?:[^{DOUBLE}`]|`.?|[{DOUBLE}]{{2}})*(?:[{DOUBLE}]|\Z)",
    re.DOTALL,
)
"""A PowerShell string or comment: a here-string or a block comment first, then a line
comment or a string in straight or curly quotes, each to its end or the end of the fence."""
OPENING_IF = re.compile(
    r"(?P<opens>(?:(?<!`\n)(?<!`\r\n)^|;)[ \t]*if\b)|if", re.MULTILINE | re.IGNORECASE
)
"""An ``if``, with what comes before it where it opens a statement: a line's start, unless
a backtick at the end of the line before carries that line on, or a ``;``."""


def top_level(code: str) -> str:
    """``code`` with each ``if`` inside braces blanked, since a block may never run."""
    depth, parts = 0, []
    for part in re.split(r"([{}])", code):
        if part in ("{", "}"):
            depth = depth + 1 if part == "{" else max(depth - 1, 0)
        elif depth:
            part = re.sub(r"(?i)if", "__", part)
        parts.append(part)
    return "".join(parts)


def executed(fence: str) -> str:
    """``fence`` with only what PowerShell runs as a statement left to read: a fence in
    another language, each string and comment, and an ``if`` that opens no statement at the
    top level are blanked one character for another, so a position in it is a position in
    ``fence``."""
    named = re.match(r"[ \t]*(?:`{3,}|~{3,})[ \t]*([\w-]*)", fence)
    if named is None or named.group(1).lower() not in POWERSHELL:
        return re.sub(r"\S", "_", fence)
    code = STRING.sub(lambda found: re.sub(r"\S", "_", found.group()), fence)
    return top_level(OPENING_IF.sub(lambda found: found["opens"] or "__", code))


def section_with_commands(document: str, anchor: str, *, run: bool = False) -> str:
    """`section_of`, with the section's fenced commands kept where they stand, less their
    comments, which run nothing. With ``run``, the prose is blanked and each fence read by
    `executed`, so only the statements that run are left, where they stand."""
    fences: list[str] = []

    def held(found: re.Match[str]) -> str:
        fence = COMMENT.sub("", found.group())
        fences.append(executed(fence) if run else fence)
        return f"<fence {len(fences) - 1}>"

    section = section_of(FENCE.sub(held, document), anchor)
    if run:
        section = re.sub(r"(?P<fence><fence \d+>)|\S", lambda found: found["fence"] or "_", section)
    return re.sub(r"<fence (\d+)>", lambda found: fences[int(found.group(1))], section)


MAKE_SETTINGS = re.compile(
    r"if \(-not \(Test-Path \.env\)\) \{ Copy-Item \.env\.example \.env \}", re.IGNORECASE
)
"""The household's settings file made from the example, never over one already there."""
READS_SETTINGS = re.compile(
    r"(?:Get-Content|gc|cat|type)\s+(?:-Path\s+)?['\"]?(?:\.[\\/])?\.env(?![\w.])"
    r"|--env-file[=\s]+['\"]?(?:\.[\\/])?\.env(?![\w.])|lines in `\.env`",
    re.IGNORECASE,
)
"""A step that reads `.env`: a command that prints or loads it, or the checklist of its lines."""
COPIES = re.compile(r"(?<![\w-])(?:copy-item|cpi|copy|cp)(?![\w-])", re.IGNORECASE)
"""A copy command, by its name or a short name PowerShell or a shell gives it."""


def check_household_settings(readme: str, guide: str) -> None:
    """Assert that the README's household link lands on a section that makes `.env` from the
    example, in a PowerShell statement that runs, before any step reads it, and whose
    commands copy nothing over a `.env` already there."""
    anchors = [anchor for anchor in GUIDE_LINK.findall(readme) if "household" in anchor]
    assert anchors, "the README links to no household section of the guide"
    for anchor in anchors:
        section = section_with_commands(guide, anchor)
        steps = " ".join(section.split())
        runs = " ".join(section_with_commands(guide, anchor, run=True).split())
        commands = " ".join(" ".join(found.group().split()) for found in FENCE.finditer(section))
        made = MAKE_SETTINGS.search(runs)
        read = READS_SETTINGS.search(steps)
        assert made, f"#{anchor} doesn't make `.env` from the example"
        assert read is None or made.start() < read.start(), (
            f"#{anchor} reads `.env` before making it"
        )
        assert len(COPIES.findall(commands)) == len(MAKE_SETTINGS.findall(commands)), (
            f"#{anchor} copies over a `.env` already there"
        )


def test_the_household_setup_makes_its_settings_file_before_reading_it() -> None:
    readme = (REPO_ROOT / "README.md").read_text(encoding="utf-8")
    guide = (REPO_ROOT / "docs" / "development.md").read_text(encoding="utf-8")
    check_household_settings(readme, guide)


HOUSEHOLD_LINK = "Follow the [household guide](docs/development.md#running-for-the-household).\n"
MAKE_IT = (
    "```powershell\nif (-not (Test-Path .env)) {\n    Copy-Item .env.example .env\n}\n\n"
    "notepad .env\n```\n\n"
)
CHECK_IT = "Then check these lines in `.env`:\n\n- `BLOSSOM_TODAY` is blank.\n\n"
LAUNCH_IT = "```powershell\nGet-Content .env |\n    ForEach-Object { $_ }\n```\n"

READS_BEFORE_MAKING = {
    "never-made": ("## Running for the household\n\n" + CHECK_IT + LAUNCH_IT, HOUSEHOLD_LINK),
    "made-after-the-launch": (
        "## Running for the household\n\n" + LAUNCH_IT + "\n" + MAKE_IT,
        HOUSEHOLD_LINK,
    ),
    "made-after-the-checklist": (
        "## Running for the household\n\n" + CHECK_IT + MAKE_IT + LAUNCH_IT,
        HOUSEHOLD_LINK,
    ),
    "made-before-a-uv-launch-only-in-another-section": (
        "## Using the planner\n\n" + MAKE_IT + "## Running for the household\n\n"
        "```bash\nuv run --env-file .env uvicorn blossom.app:app\n```\n",
        HOUSEHOLD_LINK,
    ),
    "made-over-the-household-file": (
        "## Running for the household\n\n```powershell\nCopy-Item .env.example .env\n```\n\n"
        + CHECK_IT
        + LAUNCH_IT,
        HOUSEHOLD_LINK,
    ),
    "copied-over-it-again-later": (
        "## Running for the household\n\n"
        + MAKE_IT
        + CHECK_IT
        + "```powershell\nCopy-Item -Force .env.example .env\n```\n\n"
        + LAUNCH_IT,
        HOUSEHOLD_LINK,
    ),
    "a-creation-that-is-only-a-comment": (
        "## Running for the household\n\n```powershell\n"
        "# if (-not (Test-Path .env)) { Copy-Item .env.example .env }\nnotepad .env\n```\n\n"
        + LAUNCH_IT,
        HOUSEHOLD_LINK,
    ),
    "read-from-the-current-folder-first": (
        "## Running for the household\n\n```powershell\nGet-Content .\\.env\n```\n\n"
        + MAKE_IT
        + CHECK_IT,
        HOUSEHOLD_LINK,
    ),
    "copied-over-it-in-lowercase": (
        "## Running for the household\n\n"
        + MAKE_IT
        + CHECK_IT
        + "```powershell\ncopy-item -Force .env.example .env\n```\n\n"
        + LAUNCH_IT,
        HOUSEHOLD_LINK,
    ),
    "copied-over-it-with-cp": (
        "## Running for the household\n\n"
        + MAKE_IT
        + "```bash\ncp .env.example .env\n```\n\n"
        + LAUNCH_IT,
        HOUSEHOLD_LINK,
    ),
    "copied-over-it-in-an-indented-fence": (
        "## Running for the household\n\n1. Make it:\n\n   ```powershell\n"
        "   if (-not (Test-Path .env)) { Copy-Item .env.example .env }\n   ```\n\n"
        "2. Then:\n\n   ```powershell\n   Copy-Item -Force .env.example .env\n   ```\n",
        HOUSEHOLD_LINK,
    ),
    "a-uv-launch-reading-it-first-from-the-current-folder": (
        "## Running for the household\n\n```powershell\n"
        "uv run --env-file .\\.env uvicorn blossom.app:app\n```\n\n" + MAKE_IT + CHECK_IT,
        HOUSEHOLD_LINK,
    ),
    "no-household-link": (
        "## Running for the household\n\n" + MAKE_IT + CHECK_IT + LAUNCH_IT,
        "Follow the [guide](docs/development.md#the-sample-week).\n",
    ),
    "the-heading-in-a-fence-left-open": (
        "~~~\n## Running for the household\n\n" + MAKE_IT + CHECK_IT + LAUNCH_IT,
        HOUSEHOLD_LINK,
    ),
    "copied-over-it-in-a-fence-left-open": (
        "## Running for the household\n\n"
        + MAKE_IT
        + CHECK_IT
        + "```powershell\nCopy-Item .env.example .env\n",
        HOUSEHOLD_LINK,
    ),
    "copied-over-it-in-a-list-step": (
        "## Running for the household\n\n"
        + MAKE_IT
        + CHECK_IT
        + "1. Then:\n\n    ```powershell\n    Copy-Item -Force .env.example .env\n    ```\n",
        HOUSEHOLD_LINK,
    ),
    "copied-over-it-in-a-nested-list-step": (
        "## Running for the household\n\n"
        + MAKE_IT
        + CHECK_IT
        + "1. Then:\n\n   - again:\n\n     ```powershell\n"
        "     Copy-Item .env.example .env\n     ```\n",
        HOUSEHOLD_LINK,
    ),
    "copied-over-it-past-a-deeper-run": (
        "## Running for the household\n\n"
        + MAKE_IT
        + CHECK_IT
        + " ```powershell\n# first\n    ```\nCopy-Item -Force .env.example .env\n ```\n",
        HOUSEHOLD_LINK,
    ),
    "copied-over-it-past-a-deeper-run-in-a-list-step": (
        "## Running for the household\n\n"
        + MAKE_IT
        + CHECK_IT
        + "1. Then:\n\n    ```powershell\n    # first\n       ```\n"
        "    Copy-Item -Force .env.example .env\n    ```\n",
        HOUSEHOLD_LINK,
    ),
    "made-only-in-a-quoted-string": (
        "## Running for the household\n\n```powershell\n"
        "Write-Output 'if (-not (Test-Path .env)) { Copy-Item .env.example .env }'\n"
        "Get-Content .env\n```\n",
        HOUSEHOLD_LINK,
    ),
    "made-only-in-a-double-quoted-string": (
        "## Running for the household\n\n```powershell\n"
        'Write-Output "if (-not (Test-Path .env)) { Copy-Item .env.example .env }"\n```\n\n'
        + CHECK_IT,
        HOUSEHOLD_LINK,
    ),
    "made-only-in-a-string-across-lines": (
        "## Running for the household\n\n```powershell\n$steps = '\n"
        "if (-not (Test-Path .env)) { Copy-Item .env.example .env }\n'\n```\n\n" + CHECK_IT,
        HOUSEHOLD_LINK,
    ),
    "made-only-in-a-here-string": (
        "## Running for the household\n\n```powershell\n@'\n"
        "if (-not (Test-Path .env)) { Copy-Item .env.example .env }\n'@\n```\n\n" + CHECK_IT,
        HOUSEHOLD_LINK,
    ),
    "made-only-in-a-here-string-with-an-apostrophe": (
        "## Running for the household\n\n```powershell\n@'\nit's here\n"
        "if (-not (Test-Path .env)) { Copy-Item .env.example .env }\n'@\n```\n\n" + CHECK_IT,
        HOUSEHOLD_LINK,
    ),
    "made-only-in-curly-quotes-across-lines": (
        "## Running for the household\n\n```powershell\nWrite-Output \u2018\n"
        "if (-not (Test-Path .env)) { Copy-Item .env.example .env }\n\u2019\n```\n\n" + CHECK_IT,
        HOUSEHOLD_LINK,
    ),
    "made-only-as-an-argument": (
        "## Running for the household\n\n```powershell\n"
        "Write-Output if (-not (Test-Path .env)) { Copy-Item .env.example .env }\n```\n\n"
        + CHECK_IT,
        HOUSEHOLD_LINK,
    ),
    "made-only-in-a-bash-fence": (
        "## Running for the household\n\n```bash\n"
        "if (-not (Test-Path .env)) { Copy-Item .env.example .env }\n```\n\n" + CHECK_IT,
        HOUSEHOLD_LINK,
    ),
    "made-only-in-the-prose": (
        "## Running for the household\n\n"
        "Run `if (-not (Test-Path .env)) { Copy-Item .env.example .env }` first.\n\n" + CHECK_IT,
        HOUSEHOLD_LINK,
    ),
    "made-in-a-string-then-read-then-made": (
        "## Running for the household\n\n```powershell\n"
        "Write-Output 'if (-not (Test-Path .env)) { Copy-Item .env.example .env }'\n```\n\n"
        + LAUNCH_IT
        + "\n"
        + MAKE_IT,
        HOUSEHOLD_LINK,
    ),
    "made-after-a-string-left-open": (
        "## Running for the household\n\n```powershell\nWrite-Output 'x\n"
        "if (-not (Test-Path .env)) { Copy-Item .env.example .env }\n```\n\n" + CHECK_IT,
        HOUSEHOLD_LINK,
    ),
    "made-as-an-argument-after-a-semicolon": (
        "## Running for the household\n\n```powershell\nSet-Location .; "
        "Write-Output if (-not (Test-Path .env)) { Copy-Item .env.example .env }\n```\n\n"
        + CHECK_IT,
        HOUSEHOLD_LINK,
    ),
    "made-in-a-block-comment": (
        "## Running for the household\n\n```powershell\n<#\n"
        "if (-not (Test-Path .env)) { Copy-Item .env.example .env }\n#>\nGet-Content .env\n```\n",
        HOUSEHOLD_LINK,
    ),
    "made-in-a-comment-after-a-semicolon": (
        "## Running for the household\n\n```powershell\nGet-Date;# make it; "
        "if (-not (Test-Path .env)) { Copy-Item .env.example .env }\n```\n\n" + CHECK_IT,
        HOUSEHOLD_LINK,
    ),
    "made-in-a-function-never-called": (
        "## Running for the household\n\n```powershell\nfunction Make-Env {\n"
        "    if (-not (Test-Path .env)) { Copy-Item .env.example .env }\n}\n```\n\n" + CHECK_IT,
        HOUSEHOLD_LINK,
    ),
    "made-in-a-block-that-never-runs": (
        "## Running for the household\n\n```powershell\nif ($false) {\n"
        "    if (-not (Test-Path .env)) { Copy-Item .env.example .env }\n}\n```\n\n" + CHECK_IT,
        HOUSEHOLD_LINK,
    ),
    "made-as-an-argument-on-a-carried-line": (
        "## Running for the household\n\n```powershell\nWrite-Output `\n"
        "if (-not (Test-Path .env)) { Copy-Item .env.example .env }\n```\n\n" + CHECK_IT,
        HOUSEHOLD_LINK,
    ),
    "made-glued-to-a-command": (
        "## Running for the household\n\n```powershell\n"
        "Write-Outputif (-not (Test-Path .env)) { Copy-Item .env.example .env }\n```\n\n"
        + CHECK_IT,
        HOUSEHOLD_LINK,
    ),
    "made-glued-to-a-string": (
        "## Running for the household\n\n```powershell\n"
        "Write-Output 'x'if (-not (Test-Path .env)) { Copy-Item .env.example .env }\n```\n\n"
        + CHECK_IT,
        HOUSEHOLD_LINK,
    ),
    "made-after-a-double-quoted-string-left-open": (
        '## Running for the household\n\n```powershell\nWrite-Output "x\n'
        "if (-not (Test-Path .env)) { Copy-Item .env.example .env }\n```\n\n" + CHECK_IT,
        HOUSEHOLD_LINK,
    ),
}
MAKES_BEFORE_READING = {
    "made-first": ("## Running for the household\n\n" + MAKE_IT + CHECK_IT + LAUNCH_IT),
    "made-on-one-line": (
        "## Running for the household\n\n```powershell\n"
        "if (-not (Test-Path .env)) { Copy-Item .env.example .env }\n```\n\n" + CHECK_IT
    ),
    "a-comment-beside-the-creation": (
        "## Running for the household\n\n```powershell\n# make it once\n"
        "if (-not (Test-Path .env)) { Copy-Item .env.example .env }\n```\n\n" + CHECK_IT
    ),
    "made-in-lowercase": (
        "## Running for the household\n\n```powershell\n"
        "if (-not (test-path .env)) { copy-item .env.example .env }\n```\n\n" + CHECK_IT
    ),
    "copy-named-in-the-prose": (
        "## Running for the household\n\n"
        + MAKE_IT
        + "Elsewhere, copy `.env.example` to `.env` the same way.\n\n"
        + CHECK_IT
    ),
    "made-before-a-uv-launch": (
        "## Running for the household\n\n"
        + MAKE_IT
        + CHECK_IT
        + "```bash\nuv run --env-file .env uvicorn blossom.app:app\n```\n"
    ),
    "made-first-with-crlf": (
        "## Running for the household\n\n" + MAKE_IT + CHECK_IT + LAUNCH_IT
    ).replace("\n", "\r\n"),
    "made-on-one-line-then-read": (
        "## Running for the household\n\n```powershell\n"
        "if (-not (Test-Path .env)) { Copy-Item .env.example .env }\nGet-Content .env\n```\n"
    ),
    "a-string-beside-the-creation": (
        "## Running for the household\n\n```powershell\nWrite-Output 'making .env'\n"
        "if (-not (Test-Path .env)) { Copy-Item .env.example .env }\n```\n\n" + CHECK_IT
    ),
    "an-apostrophe-in-a-string-before-the-creation": (
        '## Running for the household\n\n```powershell\nWrite-Output "it\'s time"\n'
        "if (-not (Test-Path .env)) { Copy-Item .env.example .env }\n```\n\n" + CHECK_IT
    ),
    "made-after-another-statement-on-its-line": (
        "## Running for the household\n\n```powershell\n"
        "Set-Location .; if (-not (Test-Path .env)) { Copy-Item .env.example .env }\n```\n\n"
        + CHECK_IT
    ),
    "made-in-a-pwsh-fence": (
        "## Running for the household\n\n```pwsh\n"
        "if (-not (Test-Path .env)) { Copy-Item .env.example .env }\n```\n\n" + CHECK_IT
    ),
    "made-in-a-capitalized-fence": (
        "## Running for the household\n\n```PowerShell\n"
        "IF (-not (Test-Path .env)) { Copy-Item .env.example .env }\n```\n\n" + CHECK_IT
    ),
    "a-copy-named-in-a-comment": (
        "## Running for the household\n\n```powershell\n"
        "# Copy-Item .env.example .env would replace it\n"
        "if (-not (Test-Path .env)) { Copy-Item .env.example .env }\n```\n\n" + CHECK_IT
    ),
    "made-after-a-carried-line": (
        "## Running for the household\n\n```powershell\nSet-Location `\n    .\n"
        "if (-not (Test-Path .env)) { Copy-Item .env.example .env }\n```\n\n" + CHECK_IT
    ),
    "made-after-another-block": (
        "## Running for the household\n\n```powershell\nif ($true) { Get-Date }\n"
        "if (-not (Test-Path .env)) { Copy-Item .env.example .env }\n```\n\n" + CHECK_IT
    ),
}


@pytest.mark.parametrize(
    ("guide", "readme"), READS_BEFORE_MAKING.values(), ids=READS_BEFORE_MAKING.keys()
)
def test_a_household_section_that_reads_settings_it_never_made_fails(
    guide: str, readme: str
) -> None:
    with pytest.raises(AssertionError):
        check_household_settings(readme, guide)


@pytest.mark.parametrize("guide", MAKES_BEFORE_READING.values(), ids=MAKES_BEFORE_READING.keys())
def test_a_household_section_that_makes_its_settings_first_passes(guide: str) -> None:
    check_household_settings(HOUSEHOLD_LINK, guide)


SOURCE_DATES = (
    "If the sources disagree about a due date, the call marks it with a label. What each "
    "source says goes in only when the dates they give contradict the record: none of them "
    "is the recorded due date, or the record has no due date."
)
"""When the planner is told each source's date, as `prompts.contradictions_block` decides."""
KEPT_OUT = (
    "The note's original text and its history never go",
    "Her requests for help and her reports about turning work in stay out of every call.",
)


def test_the_guide_says_when_the_planner_is_told_each_source() -> None:
    guide = (REPO_ROOT / "docs" / "development.md").read_text(encoding="utf-8")
    shares = " ".join(section_of(guide, "what-the-planner-shares").split())
    assert SOURCE_DATES in shares
    assert "disagree about a date, the call says what each source says" not in shares
    for sentence in KEPT_OUT:
        assert sentence in shares


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
