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

import functools
import hashlib
import pathlib
import re
import tomllib
from collections.abc import Callable

import pytest

from blossom.settings import PACKAGE_ROOT, REPOSITORY_ROOT
from blossom.templating import page_templates
from tests.support import (
    HER_PAGE,
    PAGE_HEADERS,
    browser,
    client_for,
    elements_of,
    signed_in_household,
)

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
PARAGRAPH = re.compile(r"<p\b[^>]*>(.*?)</p>", re.DOTALL)


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


def footer_pages(tmp_path: pathlib.Path) -> dict[str, str]:
    """Her week and the family page with the sign-in off, and the sign-in page with it on,
    as each is served."""
    with browser() as client:
        pages = {path: client.get(path, headers=PAGE_HEADERS) for path in (HER_PAGE, "/parent")}
    with client_for(signed_in_household(tmp_path)) as client:
        pages["/sign-in"] = client.get("/sign-in", headers=PAGE_HEADERS)
    for path, page in pages.items():
        assert page.status_code == 200, path
    return {path: page.text for path, page in pages.items()}


def test_every_page_links_once_to_its_source(tmp_path: pathlib.Path) -> None:
    """Each page links once from the footer to the source the templates are given, a public
    address on the web."""
    source = page_templates().env.globals["source_url"]
    assert isinstance(source, str)
    assert source.startswith("https://")
    for path, page in footer_pages(tmp_path).items():
        assert source_links(page) == [source], path
        assert page.count("Source code") == 1, path


def test_the_source_link_is_a_line_of_its_own(tmp_path: pathlib.Path) -> None:
    """The Source code link is the whole of its footer paragraph, with no period after it,
    and the sentence on the license before it keeps its own period."""
    for path, page in footer_pages(tmp_path).items():
        (footer,) = FOOTER.findall(page)
        paragraphs = [text.strip() for text in PARAGRAPH.findall(footer)]
        (at,) = [index for index, text in enumerate(paragraphs) if SOURCE_LINK.search(text)]
        assert SOURCE_LINK.fullmatch(paragraphs[at]), (path, paragraphs[at])
        assert at > 0, path
        assert paragraphs[at - 1] == "Blossom is free software under the GNU AGPL.", path


STYLESHEET = PACKAGE_ROOT / "static" / "blossom.css"

COMMENT_OR_STRING = re.compile(
    r"/\*.*?\*/|(\"(?:[^\"\\\n\r\f]|\\.)*\"|'(?:[^'\\\n\r\f]|\\.)*')|[\"']", re.DOTALL
)
"""A comment, a string, or a quote that opens no string a browser can read."""
GROUPS = ("@media", "@supports", "@container")
"""At-rules whose rules apply under a condition, which is pinned with each rule."""
SKIPPED = ("@keyframes", "@-webkit-keyframes")
"""At-rules that hold no rules for elements."""
FONT_FACE = "@font-face"
"""Kept with the rules that reach the link, since a font face changes how its text is drawn."""
CSS_SPACE = " \t\n\r\f"
VAR_NAME = re.compile(r"var\(\s*(--[\w-]+)")
UNQUOTED_URL = re.compile(r"url\(\s*[^\s'\"]", re.IGNORECASE)
"""A ``url()`` without quotes, in which a browser reads ``/*`` as part of the address."""

CHECK = (
    "A rule that can reach the footer's Source code link changed or appeared. Check the footer "
    "in a browser, at 320 px wide with 200% text and at 1440 px: the link is at least 44 px "
    "tall and its focus outline covers no other text. Then copy the rules found into "
    "FOOTER_RULES."
)

FOOTER_RULES = (
    '@font-face { font-family: "Quicksand"; '
    'src: url("/static/fonts/Quicksand-Variable.ttf") format("truetype"); '
    "font-weight: 300 700; font-style: normal; font-display: swap }",
    '@font-face { font-family: "Outfit"; '
    'src: url("/static/fonts/Outfit-Variable.ttf") format("truetype"); '
    "font-weight: 300 700; font-style: normal; font-display: swap }",
    ":root { --canvas: #fdf5f7; --rule: rgba(155, 184, 211, 0.1); "
    "--blue-action: #4c7193; --text: #4a5c6f; --text-soft: #56687a; "
    '--font-body: "Outfit", "Rubik", "Quicksand", system-ui, sans-serif }',
    "* { box-sizing: border-box }",
    "html { color-scheme: light }",
    "body { margin: 0; min-height: 100vh; font-family: var(--font-body); "
    "font-weight: 400; color: var(--text); line-height: 1.6; "
    "background: repeating-linear-gradient(to bottom, transparent 0 2rem, "
    "var(--rule) 2rem calc(2rem + 1px)), var(--canvas) }",
    "input:focus-visible, textarea:focus-visible, select:focus-visible, "
    "button:focus-visible, a:focus-visible, "
    "summary:focus-visible { outline: 3px solid var(--blue-action); outline-offset: 2px }",
    ".colophon { max-width: 46rem; margin: 0 auto; padding: 0 1.25rem 2rem; "
    "color: var(--text-soft); font-size: 0.9rem; text-align: center }",
    ".colophon a { display: inline-block; padding: 0.8rem 0; margin: 5px 0; "
    "scroll-margin: 5px 0; color: var(--blue-action) }",
)
"""Every rule that can reach the footer's link, with the custom properties it uses and the
font faces, as the stylesheet writes them: what the footer was checked with in a browser."""


def flat(text: str) -> str:
    return " ".join(text.split())


def split_top(text: str, at: str) -> list[str]:
    """``text`` split at each ``at`` outside brackets and strings, each part flattened, empty
    ones left out."""
    shadow = COMMENT_OR_STRING.sub(lambda found: "_" * len(found.group(0)), text)
    parts, depth, start = [], 0, 0
    for index, character in enumerate(shadow):
        depth += 1 if character in "([" else -1 if character in ")]" else 0
        if character == at and depth == 0:
            parts.append(text[start:index])
            start = index + 1
    return [flat(part) for part in [*parts, text[start:]] if part.strip()]


def uncommented(found: re.Match[str]) -> str:
    """A string as it is, or a comment as a space, which is what a comment is to a browser
    when a space is next to it. A comment between two tokens fails, and so does a quote that
    opens no string a browser can read."""
    if found.group(1):
        return found.group(1)
    text, (start, end) = found.string, found.span()
    assert found.group(0).startswith("/*"), f"unclosed string at {start}"
    assert text[start - 1 : start] in CSS_SPACE or text[end : end + 1] in CSS_SPACE, (
        f"comment between two tokens at {start}"
    )
    return " "


def css_rules(css: str) -> list[tuple[str, str, list[str]]]:
    """Every style rule as the conditions around it, its selector list, and its declarations.
    Anything else fails: a ``;`` outside every block or directly inside a group, which a
    browser reads as the end of an at-rule or as part of the next selector, a nested rule, an
    at-rule outside GROUPS and SKIPPED, a statement such as ``@import``, an unclosed brace,
    bracket, string or comment, a brace inside brackets, an escape, a ``url()`` without
    quotes, and, outside strings, ``<!--``, ``-->`` or a space a browser doesn't read as one."""
    assert "\\" not in css, "escape"
    assert not UNQUOTED_URL.search(css), "url() without quotes"
    text = COMMENT_OR_STRING.sub(uncommented, css)
    rules: list[tuple[str, str, list[str]]] = []
    heads: list[str] = []
    closers: list[str] = []
    start = index = 0
    while index < len(text):
        character = text[index]
        if character in "\"'":
            quoted = COMMENT_OR_STRING.match(text, index)
            assert quoted, f"unclosed string at {index}"
            index = quoted.end()
            continue
        assert not text.startswith(("/*", "<!--", "-->"), index), f"comment mark at {index}"
        assert not character.isspace() or character in CSS_SPACE, f"odd space at {index}"
        if character in "([":
            closers.append(")" if character == "(" else "]")
        elif character in ")]":
            assert closers[-1:] == [character], f"stray {character} at {index}"
            closers.pop()
        assert not (closers and character in "{}"), f"brace inside brackets at {index}"
        assert not (character == ";" and (not heads or heads[-1].startswith(GROUPS))), (
            f"semicolon between rules at {index}"
        )
        if character == "{":
            assert not heads or heads[-1].startswith(GROUPS + SKIPPED), (
                f"rule nested in {heads[-1]}"
            )
            heads.append(flat(text[start:index]))
            start = index + 1
        elif character == "}":
            assert heads, f"closing brace with nothing open at {index}"
            head = heads.pop()
            if any(above.startswith(SKIPPED) for above in [*heads, head]):
                pass
            elif head.startswith("@") and head != FONT_FACE:
                assert head.startswith(GROUPS), f"at-rule: {head}"
            else:
                rules.append((" / ".join(heads), head, split_top(text[start:index], ";")))
            start = index + 1
        index += 1
    assert not heads, f"the sheet ends inside {heads[-1] if heads else ''}"
    assert not text[start:].strip(), f"the sheet ends with {flat(text[start:])}"
    return rules


Chain = tuple[tuple[str, frozenset[str], str | None], ...]


def may_match(compound: str, chain: Chain) -> bool:
    """Whether one compound selector could match the link or an element around it, by tag,
    classes and id alone. Everything else is set aside, which can only widen the match."""
    bare = re.sub(r"::?[\w-]+", "", compound)
    tag = re.match(r"\*|[\w-]*", bare).group(0).lower()  # type: ignore[union-attr]
    classes = set(re.findall(r"\.([\w-]+)", bare))
    ids = set(re.findall(r"#([\w-]+)", bare))
    return any(
        tag in ("", "*", name) and classes <= have and ids <= {own} for name, have, own in chain
    )


def may_reach(one: str, chain: Chain) -> bool:
    """Whether a selector could style the link or an element around it. It cannot only when
    a compound that has to match the link or an element above it matches none of them; a
    compound before ``+`` or ``~`` matches a sibling and is passed over. What brackets and
    strings hold is set aside first. A selector with a namespace, ``&`` or a bracket inside a
    bracket of its kind counts as reaching."""
    one = COMMENT_OR_STRING.sub('""', one)
    if re.search(r"[|&]|\([^)]*\(|\[[^\]]*\[", one):
        return True
    bare = re.sub(r"\[[^\]]*\]|\([^()]*\)", "", one)
    pieces = [piece for piece in re.split(r"\s*([>+~])\s*|\s+", bare.strip()) if piece]
    for place, piece in enumerate(pieces):
        after = pieces[place + 1] if place + 1 < len(pieces) else ""
        if piece not in (">", "+", "~") and after not in ("+", "~") and not may_match(piece, chain):
            return False
    return True


def footer_rules(css: str, chain: Chain) -> list[str]:
    """The rules of ``css`` that can reach the footer's link, each written as one line, with
    only the custom properties the others use, directly or through another."""
    reaching = [
        rule
        for rule in css_rules(css)
        if rule[1] == FONT_FACE or any(may_reach(one, chain) for one in split_top(rule[1], ","))
    ]
    declared = [line for _, _, lines in reaching for line in lines]
    named = {
        name for line in declared if not line.startswith("--") for name in VAR_NAME.findall(line)
    }
    while (
        more := {
            name
            for line in declared
            if property_of(line) in named
            for name in VAR_NAME.findall(line)
        }
        - named
    ):
        named |= more
    found = []
    for conditions, head, lines in reaching:
        kept = [line for line in lines if not line.startswith("--") or property_of(line) in named]
        if kept:
            where = f"{conditions} / " if conditions else ""
            found.append(f"{where}{head} {{ {'; '.join(kept)} }}")
    return found


def property_of(declaration: str) -> str:
    return declaration.split(":", 1)[0].strip()


def chain_of(page: str) -> Chain:
    """The footer's Source code link and every element above it, as tag, classes and id."""
    (link,) = [
        one
        for one in elements_of(page)
        if one.tag == "a"
        and one.text.strip() == "Source code"
        and any(above.tag == "footer" for above in one.ancestors())
    ]
    around = [link, *link.ancestors()]
    assert not [one.tag for one in around if "style" in one.attributes], "inline style"
    return tuple((one.tag, one.classes, one.attributes.get("id")) for one in around)


@functools.cache
def her_week_chain() -> Chain:
    with browser() as client:
        return chain_of(client.get(HER_PAGE, headers=PAGE_HEADERS).text)


def check_footer_rules(css: str) -> None:
    assert footer_rules(css, her_week_chain()) == list(FOOTER_RULES), CHECK


def test_the_rules_that_reach_the_footer_link_are_the_checked_ones(tmp_path: pathlib.Path) -> None:
    """The footer is the same on each page, styled by the one stylesheet and nothing inline,
    and the rules that can reach its link are the ones it was checked with."""
    for path, page in footer_pages(tmp_path).items():
        assert chain_of(page) == her_week_chain(), path
        sheets = re.findall(r"<link\b[^>]*\brel=\"stylesheet\"[^>]*>", page)
        assert len(sheets) == 1, path
        assert 'href="/static/blossom.css' in sheets[0], path
        assert "<style" not in page.lower(), path
    check_footer_rules(STYLESHEET.read_text(encoding="utf-8"))


REACHES = {
    "padding": ".colophon a { padding-top: 0; }",
    "webkit-padding": ".colophon a { -webkit-padding-before: 0; -webkit-padding-after: 0; }",
    "webkit-margin": ".colophon a { -webkit-margin-before: 0; -webkit-margin-after: 0; }",
    "webkit-height": ".colophon a { -webkit-logical-height: 0; }",
    "clipped-footer": "footer { overflow: hidden; max-height: 1px; }",
    "margin-by-another-selector": "footer.colophon a { margin: 0; scroll-margin: 0; }",
    "focus-outline": ".colophon a:focus-visible { outline: none; }",
    "faded-paragraph": "p { opacity: 0; }",
    "clipped-body": "body { overflow: hidden; max-height: 10px; }",
    "token-it-uses": ":root { --blue-action: transparent; }",
    "under-a-condition": "@media (min-width: 1px) { a { clip-path: inset(50%); } }",
    "attribute": "[href] { display: none; }",
    "after-a-sibling": "main ~ footer a { visibility: hidden; }",
    "inside-is": ":is(.colophon) a { zoom: 0.1; }",
    "root-text": "html { font-size: 1px; }",
    "everything": "* { line-height: 0; }",
    "child-of-footer": "footer > * { opacity: 0.5; }",
    "escaped-class": ".colophon\\:x { margin: 0; }",
    "nested": ".colophon { & a { margin: 0; } }",
    "layer": "@layer x { a { margin: 0; } }",
    "import": '@import url("other.css");',
    "media-statement": "@media all; .colophon a { display: none; }",
    "supports-statement": "@supports (x: y); .colophon a { display: none; }",
    "keyframes-statement": "@keyframes k; .colophon a { display: none; }",
    "statement-inside-media": "@media (min-width: 1px) { @media all; .colophon a { opacity: 0; } }",
    "statement-inside-supports": "@supports (x: y) { @keyframes k; .colophon a { opacity: 0; } }",
    "statement-inside-container": "@container (x) { @supports x; .colophon a { opacity: 0; } }",
    "property": "@property --blue-action { syntax: '*'; inherits: true; }",
    "unclosed": ".colophon a { margin: 0;",
    "list-inside-is-on-the-link": ".colophon a:is(a, button) { display: none; }",
    "list-inside-is-above-the-link": ":is(.unused, .colophon) a { display: none; }",
    "list-inside-is-on-the-footer": ".colophon:is(.x, footer) a { display: none; }",
    "list-inside-is-naming-the-footer": ":is(footer, .other) a { display: none; }",
    "attribute-with-a-flag": '.colophon a[href^="https://" i] { display: none; }',
    "sum-inside-nth-child": ".colophon a:nth-child(2n+1) { display: none; }",
    "child-inside-not": ".colophon a:not(.x > .y) { display: none; }",
    "class-inside-a-string": '.colophon a:not([title=").x"]) { display: none; }',
    "space-inside-a-string": '.colophon a:not([class~="x y"]) { display: none; }',
    "bracket-inside-a-string-in-a-list": '[title="]"], .x a, .colophon a { display: none; }',
    "bracket-inside-a-string-in-a-value": '.colophon a { --unused: "("; display: none; }',
    "escaped-token-name": ":root { --blue\\2d action: transparent; }",
    "escaped-hyphen-on-the-link": ".colophon a { --blue\\-action: transparent; }",
    "escaped-hyphen-in-a-token": ":root { --blue\\-action: transparent; }",
    "escape-before-a-declaration": ".colophon a { --unused: \\(; display: none; }",
    "closer-with-nothing-open": ".colophon a { --unused: ); display: none; }",
    "bracket-left-open": "@media (min-width: 1px {}",
    "bracket-left-open-in-a-rule": ".colophon a { --unused: (; display: none; }",
    "comment-inside-a-url": (
        ".colophon a { --unused: url( /* ); display: none; } main { --unused: */ ); }"
    ),
    "bracket-closed-by-the-other-kind": "@media (min-width: 1px] {}",
    "comment-left-open": "/* main a { margin: 0; }",
    "html-comment-mark": "--> .colophon a { display: none; }",
    "html-comment-mark-before-a-property": (
        '<!-- @property --blue-action { syntax: "<length>"; inherits: true; initial-value: 0; }'
    ),
    "form-feed-inside-a-string": '.colophon a { --unused: "\f; display: none; --x: "; }',
    "url-after-a-no-break-space": (
        'main { background: url(\xa0"x" /*); } .colophon a { display: none; } /* */ ); }'
    ),
    "font-face-for-the-footer-font": (
        '@font-face { font-family: "Outfit"; src: local("Arial"); size-adjust: 5%; }'
    ),
    "block-inside-a-font-face": "@font-face { x { } }",
    "quote-that-opens-no-string": '.x { y: "a /*\n} .colophon a { display: none; } /* */ " }',
    "escape-before-a-new-line-in-a-string": (
        '.x { y: "a/*\\41\n"; } .colophon a { display: none; } .z { w: "*/" }'
    ),
}

EDITS = {
    "comment-between-compounds": (".colophon a {", ".colophon/**/a {"),
    "comment-inside-a-sum": ("calc(2rem + 1px)", "calc(2rem/**/+/**/1px)"),
    "no-break-space-in-a-selector": (".colophon a {", ".colophon\xa0a {"),
    "no-break-space-in-a-value": (
        "outline: 3px solid var(--blue-action)",
        "outline: 3px\xa0solid var(--blue-action)",
    ),
    "html-comment-mark-before-the-footer": (".colophon {", "--> .colophon {"),
    "comment-inside-a-name": (".colophon a {", ".colo/**/phon a {"),
    "comment-inside-a-value": ("scroll-margin: 5px 0", "scroll-margin: 5/**/px 0"),
    "comment-across-a-new-line-in-a-string": (
        'font-family: "Outfit";',
        'font-family: "Out/*\n*/fit";',
    ),
    "semicolon-before-a-rule": (".colophon a {", ";\n.colophon a {"),
}
"""Changes to the stylesheet's own text that a browser reads differently."""

LEAVES = {
    "another-panel's-link": ".help-panel a.ask-again { color: red; }",
    "another-element-with-the-class": "div.colophon a { margin: 0; }",
    "links-in-main": "main a { margin: 0; }",
    "by-an-id-elsewhere": "#main p { margin: 0; }",
    "token-it-doesn't-use": ":root { --unused-token: 1px; }",
    "keyframes": "@keyframes x { from { opacity: 0; } }",
    "a-list-inside-is-elsewhere": "main :is(h1, h2) a { margin: 0; }",
    "an-attribute-flag-elsewhere": 'main a[href^="https://" i] { margin: 0; }',
    "a-bracket-inside-a-string-elsewhere": 'main a[title="("] { margin: 0; }',
    "a-comment-against-a-brace": "main a {/* note */ margin: 0; }",
    "a-transparent-token-it-doesn't-use": ":root { --unused-token: transparent; }",
    "a-semicolon-inside-a-string-elsewhere": 'main a[title=";"] { margin: 0; }',
    "a-semicolon-inside-a-comment-elsewhere": "/* a; b */ main a { margin: 0; }",
    "a-rule-under-a-condition-elsewhere": "@media (min-width: 1px) { main a { margin: 0; } }",
}


@pytest.mark.parametrize("rule", REACHES.values(), ids=REACHES.keys())
def test_a_rule_that_can_reach_the_footer_link_fails_the_check(rule: str) -> None:
    with pytest.raises(AssertionError):
        check_footer_rules(STYLESHEET.read_text(encoding="utf-8") + "\n" + rule + "\n")


@pytest.mark.parametrize(("before", "after"), EDITS.values(), ids=EDITS.keys())
def test_an_edit_a_browser_reads_differently_fails_the_check(before: str, after: str) -> None:
    css = STYLESHEET.read_text(encoding="utf-8")
    assert css.count(before) == 1
    with pytest.raises(AssertionError):
        check_footer_rules(css.replace(before, after))


@pytest.mark.parametrize("rule", LEAVES.values(), ids=LEAVES.keys())
def test_a_rule_that_cannot_reach_the_footer_link_passes(rule: str) -> None:
    check_footer_rules(STYLESHEET.read_text(encoding="utf-8") + "\n" + rule + "\n")
