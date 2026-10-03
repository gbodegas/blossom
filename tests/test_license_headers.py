# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Gerardo Bodegas Martinez
"""The license, held in place: every source file in the package and the tests opens with
its SPDX header, ``LICENSE`` is the GNU Affero General Public License, and every page's
footer links to the source, which section 13 of the license asks of a copy that serves
people over a network, with room around the link to press it and to see it focused.

The fonts under ``blossom/static/fonts/`` are not this project's work and keep their own
license, the SIL Open Font License, so the scan passes them by. The package metadata names
both licenses and lists each license text the wheel and the sdist ship. An empty file has
nothing to license and stays empty, as a package marker does."""

import hashlib
import pathlib
import re
import tomllib
from collections.abc import Callable

import pytest

from blossom.settings import PACKAGE_ROOT, REPOSITORY_ROOT
from blossom.templating import page_templates
from tests.support import HER_PAGE, PAGE_HEADERS, browser, client_for, signed_in_household

IDENTIFIER = "SPDX-License-Identifier: AGPL-3.0-or-later"
COPYRIGHT = "Copyright (C) 2026 Gerardo Bodegas Martinez"

HEADERS: dict[str, tuple[str, str]] = {
    ".py": (f"# {IDENTIFIER}", f"# {COPYRIGHT}"),
    ".html": (f"{{# {IDENTIFIER} #}}", f"{{#- {COPYRIGHT} -#}}"),
    ".css": (f"/* {IDENTIFIER} */", f"/* {COPYRIGHT} */"),
    ".js": (f"// {IDENTIFIER}", f"// {COPYRIGHT}"),
    ".svg": (f"<!-- {IDENTIFIER} -->", f"<!-- {COPYRIGHT} -->"),
}
"""The two header lines in each kind of source file's own comments. A template's second
line takes the whitespace on both sides of it, so a page renders exactly as it would
without the header."""

SCANNED = (PACKAGE_ROOT, REPOSITORY_ROOT / "tests")
THIRD_PARTY = PACKAGE_ROOT / "static" / "fonts"
STAYS_FIRST = re.compile(r"#!|#.*coding[:=]|<\?xml\b")
"""A line that has to open its file: a shebang, an encoding declaration, or an XML
declaration. The header follows it."""

AGPL_SHA256 = "0d96a4ff68ad6d4b6f1f30f713b18d5184912ba8dd389f86aa7710db079abcb0"
"""The SHA-256 of the GNU AGPL v3 text exactly as gnu.org publishes it: 34,523 bytes with
LF line endings, which ``.gitattributes`` keeps in every checkout."""

FOOTER = re.compile(r'<footer class="colophon">(.*?)</footer>', re.DOTALL)
SOURCE_LINK = re.compile(r'<a href="([^"]*)">Source code</a>')


def source_files() -> list[pathlib.Path]:
    """Every file of a kind above in the package and the tests, but the fonts and empty
    files."""
    return sorted(
        path
        for folder in SCANNED
        for path in folder.rglob("*")
        if path.suffix in HEADERS
        and path.is_file()
        and THIRD_PARTY not in path.parents
        and path.stat().st_size > 0
    )


def opening(path: pathlib.Path) -> list[str]:
    """A file's first two lines after any line that has to open it."""
    lines = path.read_text(encoding="utf-8").splitlines()
    while lines and STAYS_FIRST.match(lines[0]):
        lines = lines[1:]
    return lines[:2]


def source_links(page: str) -> list[str]:
    """The address of every Source code link in each footer of the page, footer by footer."""
    return [address for footer in FOOTER.findall(page) for address in SOURCE_LINK.findall(footer)]


def test_the_scan_finds_every_kind_of_source_file() -> None:
    """Guard against the header check passing because it found nothing to check."""
    found = source_files()
    assert {path.suffix for path in found} == set(HEADERS)
    assert len(found) >= 200
    assert not [path for path in found if THIRD_PARTY in path.parents]


def test_every_source_file_opens_with_the_license_header() -> None:
    """A file added to the package or the tests carries the header too."""
    missing = [
        path.relative_to(REPOSITORY_ROOT).as_posix()
        for path in source_files()
        if opening(path) != list(HEADERS[path.suffix])
    ]
    assert not missing, f"these files do not open with the SPDX header: {missing}"


def test_the_header_skips_a_line_that_has_to_open_its_file(tmp_path: pathlib.Path) -> None:
    """A shebang, an encoding declaration, and an XML declaration keep their place."""
    script = tmp_path / "script.py"
    script.write_text(
        "#!/usr/bin/env python\n# -*- coding: utf-8 -*-\n"
        + "\n".join(HEADERS[".py"])
        + '\n"""A script."""\n',
        encoding="utf-8",
    )
    drawing = tmp_path / "drawing.svg"
    drawing.write_text(
        '<?xml version="1.0"?>\n' + "\n".join(HEADERS[".svg"]) + "\n<svg/>\n", encoding="utf-8"
    )
    assert opening(script) == list(HEADERS[".py"])
    assert opening(drawing) == list(HEADERS[".svg"])


def check_license(root: pathlib.Path) -> None:
    """Assert that ``LICENSE`` under ``root`` holds the whole AGPL text, byte for byte."""
    digest = hashlib.sha256((root / "LICENSE").read_bytes()).hexdigest()
    assert digest == AGPL_SHA256, "LICENSE isn't the GNU AGPL v3 text as gnu.org publishes it"


def test_the_license_is_the_gnu_affero_general_public_license() -> None:
    check_license(REPOSITORY_ROOT)


def test_an_exact_copy_of_the_license_passes(tmp_path: pathlib.Path) -> None:
    (tmp_path / "LICENSE").write_bytes((REPOSITORY_ROOT / "LICENSE").read_bytes())
    check_license(tmp_path)


def section_13_removed(text: bytes) -> bytes:
    start = text.index(b"  13. Remote Network Interaction")
    return text[:start] + text[text.index(b"  14. Revised Versions") :]


def one_letter_lowered(text: bytes) -> bytes:
    at = text.index(b"Remote Network Interaction")
    return text[:at] + b"r" + text[at + 1 :]


@pytest.mark.parametrize(
    "change",
    [
        lambda text: b"".join(text.splitlines(keepends=True)[:2]),
        lambda text: b"".join(text.splitlines(keepends=True)[:-1]),
        lambda text: text[:-1],
        lambda text: text[: len(text) // 2],
        lambda text: text.replace(b"must prominently offer", b"may prominently offer", 1),
        section_13_removed,
        one_letter_lowered,
        lambda text: text.replace(b"Remote Network", b"R\xe9mote Network", 1),
        lambda text: text + b"\nAdditional permission under section 7: none.\n",
        lambda text: text + b"\n",
        lambda text: text.replace(b"\n", b"\r\n"),
        lambda text: b"\xef\xbb\xbf" + text,
    ],
    ids=[
        "title-and-version-only",
        "last-line-missing",
        "final-newline-missing",
        "cut-in-half",
        "one-word-changed",
        "section-13-removed",
        "one-letter-lowered",
        "a-byte-that-is-not-utf-8",
        "terms-appended",
        "newline-appended",
        "crlf-line-endings",
        "byte-order-mark",
    ],
)
def test_a_license_cut_short_or_changed_fails(
    tmp_path: pathlib.Path, change: Callable[[bytes], bytes]
) -> None:
    original = (REPOSITORY_ROOT / "LICENSE").read_bytes()
    changed = change(original)
    assert changed != original
    (tmp_path / "LICENSE").write_bytes(changed)
    with pytest.raises(AssertionError):
        check_license(tmp_path)


def test_the_distribution_names_the_license_of_each_part_it_ships() -> None:
    """PEP 639: the wheel and the sdist carry Blossom's own code under the AGPL and the
    fonts under the OFL, so the expression names both, every license text is listed and
    present, and no license classifier repeats them."""
    pyproject = (REPOSITORY_ROOT / "pyproject.toml").read_text(encoding="utf-8")
    project = tomllib.loads(pyproject)["project"]
    assert project["license"] == "AGPL-3.0-or-later AND OFL-1.1"
    assert project["license-files"] == [
        "LICENSE",
        "blossom/static/fonts/outfit-OFL.txt",
        "blossom/static/fonts/quicksand-OFL.txt",
    ]
    missing = [name for name in project["license-files"] if not (REPOSITORY_ROOT / name).is_file()]
    assert not missing, f"listed but not in the repository: {missing}"
    assert not [
        classifier
        for classifier in project.get("classifiers", [])
        if classifier.startswith("License ::")
    ]


def test_first_party_headers_name_the_agpl_alone() -> None:
    """The OFL in the distribution's expression covers the fonts alone. Every file of
    Blossom's own says AGPL-3.0-or-later and nothing else, and the scan leaves the fonts
    and their license texts out."""
    assert IDENTIFIER == "SPDX-License-Identifier: AGPL-3.0-or-later"
    found = source_files()
    named = [path for path in found if opening(path)[:1] != [HEADERS[path.suffix][0]]]
    assert not named, f"these files name another license or none: {named}"
    assert not [path for path in found if path.is_relative_to(THIRD_PARTY)]


def test_only_the_shared_layout_writes_a_footer() -> None:
    """Every page is built on the shared layout, the pages that answer without the store
    included, so a footer written there alone is on each of them once."""
    templates = PACKAGE_ROOT / "templates"
    writers = sorted(
        path.name for path in templates.glob("*.html") if "<footer" in path.read_text("utf-8")
    )
    assert writers == ["base.html"]


def test_every_page_links_once_to_its_source(tmp_path: pathlib.Path) -> None:
    """Her week and the family page with the sign-in off, and the sign-in page with it on,
    each link once from the footer to the source the templates are given, a public address
    on the web."""
    source = page_templates().env.globals["source_url"]
    assert isinstance(source, str)
    assert source.startswith("https://")
    with browser() as client:
        pages = {path: client.get(path, headers=PAGE_HEADERS) for path in (HER_PAGE, "/parent")}
    with client_for(signed_in_household(tmp_path)) as client:
        pages["/sign-in"] = client.get("/sign-in", headers=PAGE_HEADERS)
    for path, page in pages.items():
        assert page.status_code == 200, path
        assert source_links(page.text) == [source], path
        assert page.text.count("Source code") == 1, path


STYLESHEET = PACKAGE_ROOT / "static" / "blossom.css"
FOOTER_LINK = ".colophon a"


def declared(css: str, selector: str) -> dict[str, str]:
    """The declarations every rule naming ``selector`` gives it, a later rule's over an
    earlier one's, comments left out."""
    plain = re.sub(r"/\*.*?\*/", "", css, flags=re.DOTALL)
    found: dict[str, str] = {}
    for head, inside in re.findall(r"([^{}]+)\{([^{}]*)\}", plain):
        if selector in [part.strip() for part in head.split(",")]:
            for line in inside.split(";"):
                name, _, value = line.partition(":")
                if value.strip():
                    found[name.strip()] = value.strip()
    return found


def pixels(value: str) -> float:
    if value == "0":
        return 0.0
    found = re.fullmatch(r"(\d+(?:\.\d+)?)px", value)
    assert found, f"{value!r} isn't a length in pixels"
    return float(found.group(1))


def check_footer_link(css: str) -> None:
    """Assert that the footer's link keeps its 44 pixels to press and its focus outline in
    room of its own: the padding grows its line, with no margin taking it back, and the
    margin above and below, and the room a scroll to it keeps, are at least as deep as the
    outline reaches."""
    plain = re.sub(r"/\*.*?\*/", "", css, flags=re.DOTALL)
    for head, inside in re.findall(r"([^{}]+)\{([^{}]*)\}", plain):
        if FOOTER_LINK in [part.strip() for part in head.split(",")]:
            assert "-0.8rem" not in inside, "the footer link margins its press area back in"
    link = declared(css, FOOTER_LINK)
    assert link.get("display") == "inline-block"
    assert link.get("padding") == "0.8rem 0"
    outline = declared(css, "a:focus-visible") | declared(css, f"{FOOTER_LINK}:focus-visible")
    width = pixels(outline["outline"].split()[0])
    reach = width + pixels(outline["outline-offset"])
    for name in ("margin", "scroll-margin"):
        sides = link.get(name, "0").split()
        above, below = sides[0], sides[2] if len(sides) > 2 else sides[0]
        assert min(pixels(above), pixels(below)) >= reach, f"the outline reaches past {name}"


def test_the_footer_link_keeps_its_press_area_and_outline_in_its_own_line() -> None:
    check_footer_link(STYLESHEET.read_text(encoding="utf-8"))


LINK_RULE = (
    "  display: inline-block;\n  padding: 0.8rem 0;\n  margin: 5px 0;\n  scroll-margin: 5px 0;\n"
)
FOCUS_RULE = "  outline: 3px solid var(--blue-action);\n  outline-offset: 2px;\n"


@pytest.mark.parametrize(
    ("before", "after"),
    [
        (".week-problem a {", ".week-problem a,\n.colophon a {"),
        (LINK_RULE, LINK_RULE.replace("  margin: 5px 0", "  margin: 0")),
        (LINK_RULE, LINK_RULE.replace("  margin: 5px 0", "  margin: 4px 0")),
        (LINK_RULE, LINK_RULE.replace("  margin: 5px 0", "  margin: 5px 0 0")),
        (LINK_RULE, LINK_RULE.replace("  margin: 5px 0", "  margin: -0.8rem 0")),
        (LINK_RULE, LINK_RULE.replace("scroll-margin: 5px 0", "scroll-margin: 0")),
        (LINK_RULE, LINK_RULE.replace("0.8rem 0", "0.4rem 0")),
        (LINK_RULE, LINK_RULE.replace("inline-block", "inline")),
        (FOCUS_RULE, FOCUS_RULE.replace("offset: 2px", "offset: 4px")),
        ("colophon */", "colophon */\n.colophon a:focus-visible {\n  outline: 4px solid red;\n}"),
    ],
    ids=[
        "back-in-the-sentence-rule",
        "margin-0",
        "margin-short-of-the-outline",
        "no-margin-below",
        "margin-taken-back",
        "no-room-when-scrolled-to",
        "half-the-padding",
        "inline",
        "outline-drawn-further-out",
        "a-wider-outline-here",
    ],
)
def test_a_footer_link_that_reaches_past_its_line_fails(before: str, after: str) -> None:
    original = STYLESHEET.read_text(encoding="utf-8")
    assert original.count(before) == 1
    changed = original.replace(before, after)
    with pytest.raises(AssertionError):
        check_footer_link(changed)
