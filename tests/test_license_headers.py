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

import dataclasses
import functools
import hashlib
import math
import pathlib
import re
import tomllib
from collections.abc import Callable

import pytest

from blossom.settings import PACKAGE_ROOT, REPOSITORY_ROOT
from blossom.templating import page_templates
from tests.support import (
    CSS_SPACES,
    HER_PAGE,
    KEYWORDS,
    PAGE_HEADERS,
    SUBSTITUTION,
    Element,
    Supports,
    UnreadCss,
    View,
    browser,
    client_for,
    compound,
    elements_of,
    holds,
    selector,
    signed_in_household,
    style_rules,
    unshielded,
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

FOOTER_VIEWS = [
    View(width, text, text, motion)
    for width in (320, 480, 481, 960, 961, 1152, 1153, 1440, 2304, 2305)
    for text in (16, 32)
    for motion in (False, True)
]
"""A phone to a wide screen at the browser's text size and at twice it, with and without
less motion asked for, on each side of the widths the sheet's media queries name."""

SIDES = ("top", "bottom")
BOXES = ("padding", "margin", "scroll-margin")
LINK_PROPERTIES = frozenset(
    {
        "display",
        "outline-width",
        "outline-style",
        "outline-offset",
        *(f"{box}-{side}" for box in BOXES for side in SIDES),
    }
)
"""What the footer link's room depends on: how it lays out, the top and bottom of its
padding, margin and scroll margin, and its focus outline."""

OUTLINE_WIDTHS = {"thin": "1px", "medium": "3px", "thick": "5px"}
OUTLINE_STYLES = frozenset(
    ("auto", "none", "hidden", "dotted", "dashed", "solid", "double", "groove", "ridge", "inset")
) | {"outset"}
STATES = re.compile(r":(?:hover|active|focus-visible|focus-within|focus)(?![\w-])", re.I)
ON_FOCUS = re.compile(r"(?P<before>.*?)(?P<states>(?::focus-visible|:focus)+)", re.I | re.S)
"""A selector that holds while its element has focus: `:focus` or `:focus-visible` at its
end, and no other state anywhere."""


def words_of(value: str) -> list[str]:
    """The words of a value, a function such as ``calc(1px + 2px)`` kept whole."""
    return re.findall(r"(?:[^\s(]|\([^()]*\))+", value)


def outline_parts(value: str) -> dict[str, str]:
    """The width and style an ``outline`` shorthand sets, each its initial value where it
    names none. A browser reads any other word as the color, so a `SUBSTITUTION` is read as
    the color only when the width and the style are both written. A negative width
    drops the declaration, as a browser drops it."""
    found = {"outline-width": "medium", "outline-style": "none"}
    named: set[str] = set()
    substituted = False
    for word in words_of(value):
        if word in OUTLINE_STYLES:
            found["outline-style"] = word
            named.add("style")
        elif word.startswith("-") and re.match(r"-[\d.]", word):
            return {}
        elif word in OUTLINE_WIDTHS or re.match(r"[+-]?[\d.]", word):
            found["outline-width"] = word
            named.add("width")
        elif SUBSTITUTION.search(word) or "\\" in word:
            substituted = True
    if value in KEYWORDS or (substituted and named != {"style", "width"}):
        raise UnreadCss(value)
    return found


REFUSED = "refused"
"""What `link_longhands` sets for a declaration the resolver can't weigh, which a rule that
reaches the footer link may not hold."""

BOX_WORD = re.compile(r"[+-]?(?:\d+\.?\d*|\.\d+)(?:[a-z]+|%)?|auto|[a-z-]+\(.*\)")
"""A word a browser may take in a padding or margin: a number with its unit, ``auto``, or a
function. A declaration with any other word is dropped, as a browser drops it, unless
it holds an escape, which `link_rules` refuses."""


def link_longhands(name: str, value: str) -> dict[str, str]:
    """What one declaration sets among `LINK_PROPERTIES`: a padding, margin or scroll
    margin shorthand, or its block form, the top and bottom it reaches, and a logical side
    the top or bottom it is in a page written top to bottom. ``all`` sets `REFUSED`;
    `check_root_text` refuses an escaped name and an animation."""
    if name == "all":
        return {REFUSED: name}
    if name == "outline":
        return outline_parts(value)
    if name == "outline-width" and value.startswith("-"):
        return {}
    words = words_of(value)
    if any(name.startswith(box) for box in BOXES) and not (
        value in KEYWORDS or "\\" in value or all(BOX_WORD.fullmatch(word) for word in words)
    ):
        return {}
    for box in BOXES:
        if name == box:
            if not 1 <= len(words) <= 4:
                raise UnreadCss(value)
            return {f"{box}-top": words[0], f"{box}-bottom": words[2 if len(words) > 2 else 0]}
        if name == f"{box}-block":
            if not 1 <= len(words) <= 2:
                raise UnreadCss(value)
            return {f"{box}-top": words[0], f"{box}-bottom": words[-1]}
        for side, logical in (("top", "block-start"), ("bottom", "block-end")):
            if name in (f"{box}-{side}", f"{box}-{logical}"):
                return {f"{box}-{side}": value}
    return {name: value} if name in LINK_PROPERTIES else {}


@dataclasses.dataclass(frozen=True)
class LinkRule:
    """One value a rule gives the footer link: how the cascade weighs it (important, then
    the more specific, then the later), the condition it sits under, and whether it holds
    only while the link has focus."""

    weight: tuple[bool, tuple[int, int, int], int]
    value: str
    media: str | Supports | None
    on_focus: bool


def reach_of(head: str, link: Element) -> tuple[tuple[int, int, int], bool] | None:
    """How one selector, as `style_rules` gives it, reaches ``link``: its specificity, and
    whether only while the link has focus. ``None`` where its last compound can't match the
    link in any state. A selector that would reach it in a state other than focus, or with
    a state anywhere but its end, is refused."""
    anyhow = STATES.sub("*", re.sub(r":not\([^()]*\)", "", head, flags=re.I)).strip()
    if not compound(unshielded(re.split(r"[\s>+~]+", anyhow)[-1])).matches(link):
        return None
    if not STATES.search(head):
        chosen = selector(unshielded(head))
        return (chosen.specificity, False) if chosen.matches(link) else None
    if not selector(unshielded(anyhow)).matches(link):
        return None
    focus = ON_FOCUS.fullmatch(head.strip())
    if focus is None or STATES.search(focus["before"]):
        raise UnreadCss(unshielded(head))
    before = focus["before"]
    if not before.strip() or before[-1] in f"{CSS_SPACES}>":
        before += "*"
    a, b, c = selector(unshielded(before)).specificity
    return (a, b + len(re.findall(":focus", focus["states"], re.I)), c), True


def link_rules(css: str, link: Element) -> dict[str, list[LinkRule]]:
    """Every value a rule of ``css`` gives ``link`` among `LINK_PROPERTIES`, by property,
    each declaration of a rule over an earlier one unless that one is important. A value
    the resolver cannot read, a keyword or a `SUBSTITUTION`, is refused."""
    found: dict[str, list[LinkRule]] = {}
    for style in style_rules(css):
        declared: dict[str, tuple[str, bool]] = {}
        for name, value, important in style.declarations:
            for longhand, setting in link_longhands(name, value.lower()).items():
                if important or not declared.get(longhand, ("", False))[1]:
                    declared[longhand] = (setting, important)
        if not declared:
            continue
        for head in style.selectors.split(","):
            reach = reach_of(head, link)
            if reach is None:
                continue
            if REFUSED in declared:
                raise UnreadCss(declared[REFUSED][0])
            for name, (value, important) in declared.items():
                if value in KEYWORDS or SUBSTITUTION.search(value) or "\\" in value:
                    raise UnreadCss(value)
                weight = (important, reach[0], style.order)
                found.setdefault(name, []).append(LinkRule(weight, value, style.media, reach[1]))
    return found


def link_value(
    rules: dict[str, list[LinkRule]], name: str, view: View, *, focused: bool
) -> str | None:
    """The value the cascade settles on for the footer link on ``view``, at rest or with
    focus, ``None`` where no rule gives it one."""
    found: LinkRule | None = None
    for rule in rules.get(name, []):
        if (
            (focused or not rule.on_focus)
            and holds(rule.media, view)
            and (found is None or rule.weight > found.weight)
        ):
            found = rule
    return None if found is None else found.value


def pixels(value: str, view: View) -> float:
    """A length in CSS pixels on ``view``: pixels, or a `rem` of the root's text size."""
    found = re.fullmatch(r"([+-]?(?:\d+\.?\d*|\.\d+))(px|rem)?", OUTLINE_WIDTHS.get(value, value))
    if found is None or (found.group(2) is None and float(found.group(1)) != 0):
        raise UnreadCss(value)
    return float(found.group(1)) * (view.root_text if found.group(2) == "rem" else 1)


def views_for(rules: dict[str, list[LinkRule]]) -> list[View]:
    """`FOOTER_VIEWS`, and the widths each length named by a media query around a rule that
    reaches the link comes to at either text size, rounded down, and one pixel past that,
    so a view falls in every window between two of them that holds a whole pixel."""
    widths: set[int] = set()
    for rule in (one for each in rules.values() for one in each):
        media = rule.media.media if isinstance(rule.media, Supports) else rule.media
        for number, unit in re.findall(r"(\d*\.?\d+)(px|rem|em)", (media or "").lower()):
            for text in (16, 32):
                width = float(number) * (1 if unit == "px" else text)
                widths |= {math.floor(width), math.floor(width) + 1}
    return [
        *FOOTER_VIEWS,
        *(
            View(width, text, text, motion)
            for width in sorted(widths)
            for text in (16, 32)
            for motion in (False, True)
        ),
    ]


STRING_OR_COMMENT = re.compile(
    r"\\.|\"(?:[^\"\\\n]|\\.)*\"|'(?:[^'\\\n]|\\.)*'|/\*.*?(?:\*/|\Z)", re.S
)
"""An escape, a string, or a comment, each read whole, so an escaped quote opens no string."""
DELIMITERS = "{};,>+~"


def check_comments(css: str) -> None:
    """Refuse a comment, outside a string, with no space or delimiter on either side: a
    browser joins what is written on each side of it, where the shared reader puts a space
    between them."""
    for found in STRING_OR_COMMENT.finditer(css):
        before, after = css[found.start() - 1 : found.start()], css[found.end() : found.end() + 1]
        if found.group().startswith("/*") and all(
            side.strip() and side not in DELIMITERS for side in (before, after)
        ):
            raise UnreadCss(css[found.start() - 20 : found.end() + 20])


ANIMATION = ("animation", "animation-name")


def check_root_text(css: str, link: Element) -> None:
    """Refuse a rule that sets the root element's text size, which the views give and every
    `rem` is read by, that zooms the link or an element above it, or that reaches either
    with an escaped property name or an animation, which may stand for any of these."""
    styles = style_rules(css)
    for one in (link, *link.ancestors()):
        names = {"zoom", "font", "font-size"} if one.tag == "html" else {"zoom"}
        for style in styles:
            if any(
                name in names or "\\" in name or (name in ANIMATION and value != "none")
                for name, value, _ in style.declarations
            ) and any(reach_of(head, one) is not None for head in style.selectors.split(",")):
                raise UnreadCss(style.selectors)


@functools.cache
def footer_links() -> tuple[Element, ...]:
    """The footer's link on her week and on the family page, each with the elements above
    it, for the stylesheet's selectors to match."""
    with browser() as client:
        pages = [client.get(path, headers=PAGE_HEADERS).text for path in (HER_PAGE, "/parent")]
    found: list[Element] = []
    for page in pages:
        links = [
            one
            for one in elements_of(page)
            if one.tag == "a" and any(above.tag == "footer" for above in one.ancestors())
        ]
        assert len(links) == 1
        found.append(links[0])
    return tuple(found)


def check_footer_link(css: str) -> None:
    """Assert that the footer's link keeps its 44 pixels to press and its focus outline in
    room of its own, on every view, at rest and with focus, whichever rules reach it: an
    inline block padded at least 0.8rem above and below, so the padding grows its line; no
    rule takes the press area back with a margin; and its margin and the room a scroll to
    it keeps are at least as deep above and below as its outline reaches, and the outline is
    drawn."""
    check_comments(css)
    for link in footer_links():
        check_root_text(css, link)
        rules = link_rules(css, link)
        taken = [
            rule.value
            for side in SIDES
            for rule in rules.get(f"margin-{side}", [])
            if pixels(rule.value, FOOTER_VIEWS[0]) < 0
        ]
        assert not taken, f"a rule margins the footer link's press area back in: {taken}"
        for view in views_for(rules):
            style = link_value(rules, "outline-style", view, focused=True)
            if style is None or style == "auto":
                msg = f"the browser's own focus outline on {view}"
                raise UnreadCss(msg)
            assert style not in ("none", "hidden"), f"the footer link shows no outline on {view}"
            width = link_value(rules, "outline-width", view, focused=True) or "medium"
            offset = link_value(rules, "outline-offset", view, focused=True) or "0"
            assert pixels(width, view) > 0, f"the footer link's outline has no width on {view}"
            reach = pixels(width, view) + pixels(offset, view)
            for focused in (False, True):
                seen = f"{view}, {'with focus' if focused else 'at rest'}"
                shown = link_value(rules, "display", view, focused=focused)
                assert shown == "inline-block", f"the footer link is {shown} on {seen}"
                for side in SIDES:
                    padding = link_value(rules, f"padding-{side}", view, focused=focused)
                    assert pixels(padding or "0", view) >= 0.8 * view.root_text, (
                        f"the footer link's padding {side} is short on {seen}"
                    )
                    for name in ("margin", "scroll-margin"):
                        room = link_value(rules, f"{name}-{side}", view, focused=focused)
                        assert pixels(room or "0", view) >= reach, (
                            f"the outline reaches past the {name} {side} on {seen}"
                        )


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


TAKES_THE_ROOM = {
    "longhands": ".colophon a { padding-top: 0; padding-bottom: 0; margin-top: -10px; "
    "margin-bottom: -10px; }",
    "another-selector": "footer.colophon a { margin: 0; scroll-margin: 0; }",
    "padding-below-just-short": ".colophon a { padding-bottom: 0.79rem; }",
    "padding-in-pixels": ".colophon a { padding-block: 13px; }",
    "logical-margin": ".colophon a { margin-block-end: 4px; }",
    "logical-scroll-margin": ".colophon a { scroll-margin-block: 5px 0; }",
    "longhand-after-shorthand": ".colophon a { margin: 5px 0; margin-bottom: 0; }",
    "important-but-less-specific": "footer a { margin-bottom: 0 !important; }",
    "more-specific": "footer.colophon p a { margin-top: 4px; }",
    "any-element": "* { scroll-margin: 0 !important; }",
    "names-in-capitals": ".colophon A { MARGIN: 0; }",
    "by-its-address": "footer [href] { margin: 0; }",
    "narrow-screens": "@media (max-width: 30rem) { .colophon a { margin: 0; } }",
    "less-motion": "@media (prefers-reduced-motion: reduce) { .colophon a { margin: 0; } }",
    "only-with-focus": ".colophon a:focus-visible { margin: 0; }",
    "only-with-focus-by-any-element": "footer :focus { scroll-margin: 0; }",
    "outline-offset-elsewhere": "footer a:focus-visible { outline-offset: 3px; }",
    "outline-width-alone": ".colophon a:focus-visible { outline-width: thick; }",
    "no-outline": ".colophon a:focus-visible { outline: none; }",
    "the-browser-outline": ".colophon a:focus-visible { outline-style: auto; }",
    "on-hover": ".colophon a:hover { margin: 0; }",
    "unless-focused": ".colophon a:not(:focus) { margin: 0; }",
    "a-state-above": ".colophon:focus-within a { margin: 0; }",
    "a-custom-property": ".colophon a { margin: var(--room) 0; }",
    "a-keyword": ".colophon a { margin: inherit; }",
    "everything-reset": ".colophon a { all: unset; }",
    "a-calculation": ".colophon a { scroll-margin: calc(5px - 1px) 0; }",
    "an-em": ".colophon a { margin: 1em 0; }",
    "escaped-names": ".colophon a { m\\61rgin: 0; scroll-m\\61rgin: 0; }",
    "an-animation": "@keyframes squash { from, to { margin: 0; } }\n"
    ".colophon a { animation: squash 1s infinite; }",
    "a-media-window": "@media (min-width: 600px) and (max-width: 900px) { .colophon a { "
    "margin: 0; scroll-margin: 0; } }",
    "a-media-window-in-rem": "@media (min-width: 40rem) and (max-width: 40.5rem) { "
    ".colophon a { margin: 0; } }",
    "an-outline-of-no-width": ".colophon a:focus-visible { outline-width: 0; }",
    "the-root-text-halved": "html { font-size: 50%; }",
    "the-root-font": ":root { font: 8px sans-serif; }",
    "zoomed-above": "footer { zoom: 0.5; }",
    "an-escaped-unit": ".colophon a { margin: 0\\70x 0; scroll-margin: 0\\70x 0; }",
    "an-escaped-quote-before-a-joining-comment": '.a\\"b{} footer/**/.colophon a{margin:0} '
    '.c::after{content:"x"}',
    "the-root-text-animated": "@keyframes shrink { to { font-size: 50%; } }\n"
    "html { animation: shrink 1s forwards; }",
    "a-gap-between-two-queries": ".colophon a { margin: 0; scroll-margin: 0; }\n"
    "@media (max-width: 600px) { .colophon a { margin: 5px 0; scroll-margin: 5px 0; } }\n"
    "@media (min-width: 700px) { .colophon a { margin: 5px 0; scroll-margin: 5px 0; } }",
    "a-comment-joining-a-selector": "footer/**/.colophon a { margin: 0; scroll-margin: 0; }",
    "an-outline-width-from-a-custom-property": ".colophon a:focus-visible { "
    "outline: solid var(--o); }",
    "room-only-with-focus": ".colophon a { margin: 0; }\n"
    ".colophon a:focus-visible { margin: 5px 0; }",
    "past-a-fraction-of-a-pixel": "@media (min-width: 4000.5px) { .colophon a { margin: 0; } }",
    "narrower-than-any-phone": "@media (max-width: 300.5px) { .colophon a { margin: 0; } }",
    "very-wide-screens": "@media (min-width: 200rem) { .colophon a { margin: 0; } }",
}
"""A rule added after the sheet's own, which takes some of the footer link's room on some
view or state, or which the resolver can't read and so refuses."""

LEAVES_THE_ROOM = {
    "less-specific": "footer a { margin-bottom: 0; }",
    "every-link": "a { margin: 0; padding: 0; display: inline; }",
    "shorthand-after-longhand": ".colophon a { margin-bottom: 0; margin: 5px 0; }",
    "padding-at-the-value": ".colophon a { padding-bottom: 0.8rem; }",
    "padding-just-over": ".colophon a { padding-block: 0.81rem; }",
    "margin-just-over": ".colophon a { margin-block: 6px; scroll-margin-top: 6px; }",
    "important-here": ".colophon a { margin: 5px 0 !important; }",
    "only-in-print": "@media print { .colophon a { margin: 0; } }",
    "an-equivalent-rule": "footer.colophon a { display: inline-block; padding: 0.8rem 0; "
    "margin: 5px 0; scroll-margin: 5px 0; }",
    "a-comment-beside-a-space": "footer /* not this one */ .colophon a { margin: 0; }",
    "a-comment-in-a-string": '.colophon a::after { content: "x/**/y"; }',
    "a-negative-outline-width-dropped": ".colophon a:focus-visible { outline-width: -1px; }",
    "a-negative-outline-dropped": ".colophon a:focus-visible { outline: -1px solid red; }",
    "a-comment-after-a-comma": ".places a,/* and */.colophon a { margin: 5px 0; }",
    "another-link-on-hover": ".places a:hover { margin: 0; }",
    "outline-offset-at-the-value": ".colophon a:focus-visible { outline-offset: 2px; }",
    "outline-color": ".colophon a:focus-visible { outline: 3px solid var(--text-soft); }",
    "a-declaration-a-browser-drops": ".colophon a { margin: 5px 0; margin: 0 banana; }",
    "no-animation": ".colophon a { animation: none; }",
    "an-animation-elsewhere": ".places a { animation: squash 1s; }",
    "text-size-below-the-root": "footer { font-size: 50%; }",
}
"""A rule added after the sheet's own that leaves the footer link's room as it is."""


@pytest.mark.parametrize("rule", TAKES_THE_ROOM.values(), ids=TAKES_THE_ROOM.keys())
def test_a_later_rule_that_takes_the_footer_link_room_fails(rule: str) -> None:
    css = STYLESHEET.read_text(encoding="utf-8") + "\n" + rule + "\n"
    with pytest.raises(AssertionError):
        check_footer_link(css)


@pytest.mark.parametrize("rule", LEAVES_THE_ROOM.values(), ids=LEAVES_THE_ROOM.keys())
def test_a_later_rule_that_leaves_the_footer_link_room_passes(rule: str) -> None:
    check_footer_link(STYLESHEET.read_text(encoding="utf-8") + "\n" + rule + "\n")
