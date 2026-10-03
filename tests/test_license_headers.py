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
from collections.abc import Callable, Mapping

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
    Selector,
    Supports,
    UnreadCss,
    View,
    blocks,
    browser,
    client_for,
    compound,
    elements_of,
    holds,
    importance_of,
    selector,
    shielded,
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
        "outline-color",
        "outline-offset",
        *(f"{box}-{side}" for box in BOXES for side in SIDES),
    }
)
"""What the footer link's room depends on: how it lays out, the top and bottom of its
padding, margin and scroll margin, and its focus outline and the color it's drawn in."""

OUTLINE_WIDTHS = {"thin": "1px", "medium": "3px", "thick": "5px"}
OUTLINE_STYLES = frozenset(
    ("auto", "none", "hidden", "dotted", "dashed", "solid", "double", "groove", "ridge", "inset")
) | {"outset"}
STATES = re.compile(r":(?:hover|active|focus-visible|focus-within|focus)(?![\w-])", re.I)
ON_FOCUS = re.compile(r"(?P<before>.*?)(?P<states>(?::focus-visible|:focus)+)", re.I | re.S)
"""A selector that holds while its element has focus: `:focus` or `:focus-visible` at its
end, and no other state anywhere."""

rules_of = functools.lru_cache(maxsize=4)(style_rules)
"""`style_rules`, kept for the last few sheets, which each check reads once per element."""


def words_of(value: str) -> list[str]:
    """The words of a value, a function such as ``calc(1px + 2px)`` kept whole."""
    return re.findall(r"(?:[^\s(]|\([^()]*\))+", value)


NUMBER = re.compile(r"\d+(?:\.\d+)?|\.\d+")
"""A number as CSS writes it, with no sign: digits after a point, if it has one."""
SIGNED = rf"[+-]?(?:{NUMBER.pattern})"
AMOUNT = rf"{SIGNED}%?|none"
"""A color channel or an opacity written apart by spaces: a number, a percentage, or
``none``."""
HUE = rf"{SIGNED}(?:deg|grad|rad|turn)?"
COLOR_CHANNELS: dict[str, tuple[str, ...]] = {
    **dict.fromkeys(("rgb", "rgba", "lab", "oklab"), (AMOUNT,) * 3),
    **dict.fromkeys(("hsl", "hsla", "hwb"), (rf"{HUE}|none", AMOUNT, AMOUNT)),
    **dict.fromkeys(("lch", "oklch"), (AMOUNT, AMOUNT, rf"{HUE}|none")),
    "color": (
        "srgb|srgb-linear|display-p3|a98-rgb|prophoto-rgb|rec2020|xyz|xyz-d50|xyz-d65",
        *(AMOUNT,) * 3,
    ),
}
"""The color functions the resolver reads, each with what its channels may be when they
are written apart by spaces."""
LEGACY_CHANNELS: dict[str, tuple[tuple[str, ...], ...]] = {
    **dict.fromkeys(("rgb", "rgba"), ((SIGNED,) * 3, (rf"{SIGNED}%",) * 3)),
    **dict.fromkeys(("hsl", "hsla"), ((HUE, rf"{SIGNED}%", rf"{SIGNED}%"),)),
}
"""What the channels of ``rgb()`` and ``hsl()`` may be when written apart by commas, with no
``none``: all numbers or all percentages in ``rgb()``."""
COLOR_FUNCTIONS = frozenset(COLOR_CHANNELS)
"""The only functions an ``outline`` shorthand may hold."""
UNSET_OUTLINE = {
    "outline-width": "medium",
    "outline-style": "none",
    "outline-color": "currentcolor",
}
"""What an ``outline`` shorthand sets once a substitution leaves it unreadable: each part its
initial value, as a browser sets them when it styles the page."""
VAR = re.compile(r"(?<![\w-])var\(\s*(--[\w-]+)\s*(?:,([^()]*))?\)", re.IGNORECASE)
"""A ``var()`` with the custom property it names, as written, and its fallback, if any. A
name that runs into the ``var`` before it, as in ``solidvar(``, makes another function."""
COLOR_NAMES = """aliceblue antiquewhite aqua aquamarine azure beige bisque black blanchedalmond blue
    blueviolet brown burlywood cadetblue chartreuse chocolate coral cornflowerblue
    cornsilk crimson cyan darkblue darkcyan darkgoldenrod darkgray darkgreen darkkhaki
    darkmagenta darkolivegreen darkorange darkorchid darkred darksalmon darkseagreen
    darkslateblue darkslategray darkturquoise darkviolet deeppink deepskyblue dimgray
    dodgerblue firebrick floralwhite forestgreen fuchsia gainsboro ghostwhite gold
    goldenrod gray green greenyellow honeydew hotpink indianred indigo ivory khaki
    lavender lavenderblush lawngreen lemonchiffon lightblue lightcoral lightcyan
    lightgoldenrodyellow lightgray lightgreen lightpink lightsalmon lightseagreen
    lightskyblue lightslategray lightsteelblue lightyellow lime limegreen linen magenta
    maroon mediumaquamarine mediumblue mediumorchid mediumpurple mediumseagreen
    mediumslateblue mediumspringgreen mediumturquoise mediumvioletred midnightblue
    mintcream mistyrose moccasin navajowhite navy oldlace olive olivedrab orange
    orangered orchid palegoldenrod palegreen paleturquoise palevioletred papayawhip
    peachpuff peru pink plum powderblue purple rebeccapurple red rosybrown royalblue
    saddlebrown salmon sandybrown seagreen seashell sienna silver skyblue slateblue
    slategray snow springgreen steelblue tan teal thistle tomato turquoise violet wheat
    white whitesmoke yellow yellowgreen transparent currentcolor"""
NAMED_COLORS = frozenset(COLOR_NAMES.split())
"""The colors CSS names, each gray in its American spelling only, and the two keywords
that give a color."""
HEX_COLOR = re.compile(r"#(?:[0-9a-f]{3,4}|[0-9a-f]{6}|[0-9a-f]{8})")


def negative(value: str) -> bool:
    """Whether ``value`` opens with a number below zero, which a browser drops where none is
    allowed. Minus zero is zero."""
    found = re.match(rf"-({NUMBER.pattern})", value)
    return found is not None and float(found.group(1)) > 0


def visible(color: str) -> bool:
    """Whether an outline drawn in ``color`` shows: any color but one with no opacity at all.
    The text's own color, which the resolver doesn't follow, a color made from another, and a
    color whose opacity it can't read are refused."""
    color = color.strip().lower()
    if color == "transparent":
        return False
    if color in NAMED_COLORS and color != "currentcolor":
        return True
    if HEX_COLOR.fullmatch(color):
        digits = color[1:]
        return len(digits) in (3, 6) or int(digits[len(digits) // 4 * 3 :], 16) > 0
    alpha = alpha_of(color)
    if alpha is None:
        return True
    number = re.fullmatch(rf"({SIGNED})%?", alpha)
    if number is None:
        raise UnreadCss(color)
    return float(number.group(1)) > 0


def alpha_of(color: str) -> str | None:
    """The opacity a color function gives, as written, or ``None`` where it names none. A
    function whose channels don't fit `COLOR_CHANNELS` or `LEGACY_CHANNELS`, which a
    browser drops with its declaration, is refused."""
    found = re.fullmatch(r"([a-z]+)\((.*)\)", color, re.S)
    if found is None or found.group(1) not in COLOR_CHANNELS:
        raise UnreadCss(color)
    name, inside = found.groups()
    if "," in inside:
        words = [word.strip() for word in inside.split(",")]
        alpha = words.pop() if len(words) == 4 else None
        shapes, last = LEGACY_CHANNELS.get(name, ()), rf"{SIGNED}%?"
    else:
        channels, slash, after = inside.partition("/")
        words, alpha = channels.split(), after.strip() if slash else None
        shapes, last = (COLOR_CHANNELS[name],), AMOUNT
    if not any(
        len(shape) == len(words) and all(map(re.fullmatch, shape, words)) for shape in shapes
    ) or (alpha is not None and not re.fullmatch(last, alpha)):
        raise UnreadCss(color)
    return alpha


def outline_width(word: str) -> bool | None:
    """Whether a browser takes ``word`` as an outline's width: a width keyword, zero, or a
    length of zero or more. ``None`` for a unit outside `LENGTH_UNITS` or a number this can't
    read."""
    if word in OUTLINE_WIDTHS:
        return True
    found = re.fullmatch(rf"({SIGNED})([a-z]+|%)?", word)
    if found is None:
        return None
    amount, unit = float(found.group(1)), found.group(2)
    if unit is None:
        return amount == 0
    if unit == "%":
        return False
    return None if unit not in LENGTH_UNITS else amount >= 0


def read_outline(value: str) -> dict[str, str] | None:
    """The width, style and color an ``outline`` shorthand with no substitution sets, each its
    initial value where it names none, or ``None`` where a browser can't read it: a width
    `outline_width` doesn't take, a part named twice, or a keyword among other words. An
    escape, a function other than a color `alpha_of` reads, a width in a unit or number
    `outline_width` can't read, or a word that is none of a width, a style or a color, is
    refused."""
    if "\\" in value:
        raise UnreadCss(value)
    if any(name not in COLOR_FUNCTIONS for name in re.findall(r"([\w-]*)\(", value)):
        raise UnreadCss(value)
    found = dict(UNSET_OUTLINE)
    named: set[str] = set()
    for word in words_of(value):
        if word in OUTLINE_STYLES:
            part = "outline-style"
        elif word in OUTLINE_WIDTHS or re.match(r"[+-]?[\d.]", word):
            part = "outline-width"
        elif word in KEYWORDS:
            return None
        elif "(" in word or word in NAMED_COLORS or HEX_COLOR.fullmatch(word):
            part = "outline-color"
            if "(" in word:
                alpha_of(word)
        else:
            raise UnreadCss(value)
        width = outline_width(word) if part == "outline-width" else True
        if width is None:
            raise UnreadCss(value)
        if part in named or not width:
            return None
        named.add(part)
        found[part] = word
    return {name: found[name] for name in UNSET_OUTLINE}


def substituted(value: str, tokens: Mapping[str, str | None]) -> str | None:
    """``value`` with each ``var()`` put in its place: the custom property's value from
    ``tokens``, or the fallback where ``tokens`` has none, and ``None`` where neither is given.
    Each value goes in with a space on either side, since CSS puts its tokens in place and
    never joins them to the text beside it. A token ``tokens`` can't weigh is refused."""
    parts: list[str] = []
    end = 0
    for found in VAR.finditer(value):
        name, fallback = found.group(1), found.group(2)
        if name in tokens and tokens[name] is None:
            raise UnreadCss(found.group())
        given = tokens.get(name, fallback)
        if given is None:
            return None
        parts += [value[end : found.start()], f" {given} "]
        end = found.end()
    return "".join([*parts, value[end:]])


def outline_parts(value: str, tokens: Mapping[str, str | None]) -> dict[str, str]:
    """The width, style and color an ``outline`` shorthand sets, by `read_outline`. One a browser
    can't read is dropped, as a browser drops it, unless it holds a `SUBSTITUTION`: that is
    put in its place from ``tokens`` first, and where it then can't be read, the outline is
    `UNSET_OUTLINE`. A keyword alone is refused."""
    if value.strip().lower() in KEYWORDS:
        raise UnreadCss(value)
    if not SUBSTITUTION.search(value):
        return read_outline(value.lower()) or {}
    given = substituted(value, tokens)
    return (None if given is None else read_outline(given.lower())) or dict(UNSET_OUTLINE)


@functools.lru_cache(maxsize=4)
def empty_tokens(css: str) -> frozenset[str]:
    """The custom properties a declaration of ``css`` leaves empty, which `style_rules` passes
    over and a browser puts in place as nothing at all. Font faces and keyframes are passed
    over, as `style_rules` passes them over, and an empty value under an escaped name is
    refused, since the name may be a custom property's."""
    found: set[str] = set()

    def read(part: str) -> None:
        for before, inside in blocks(part):
            keyword = re.match(r"@[\w-]*", before)
            if keyword and keyword.group().lower() in ("@font-face", "@keyframes"):
                continue
            if "{" in inside:
                read(inside)
                continue
            for line in inside.split(";"):
                name, colon, value = line.partition(":")
                name = name.strip()
                if not colon or importance_of(value.strip())[0]:
                    continue
                if "\\" in name:
                    raise UnreadCss(name)
                if name.startswith("--"):
                    found.add(name)

    read(shielded(css))
    return frozenset(found)


def root_tokens(css: str) -> dict[str, str | None]:
    """The custom properties ``css`` sets on the root, each by the cascade (important, then
    the later) among the rules for ``:root`` alone that hold everywhere, and ``None`` for one
    any other rule sets or any rule leaves empty, which the resolver can't weigh for the
    footer link."""
    given: dict[str, tuple[bool, str]] = {}
    elsewhere: set[str] = set()
    for style in rules_of(css):
        plain = style.media is None and style.selectors.strip().lower() == ":root"
        for name, value, important in style.declarations:
            if not name.startswith("--"):
                continue
            if not plain:
                elsewhere.add(name)
            elif important or not given.get(name, (False, ""))[0]:
                given[name] = (important, value.strip())
    return {name: value for name, (_, value) in given.items()} | dict.fromkeys(
        elsewhere | empty_tokens(css)
    )


REFUSED = "refused"
"""What `link_longhands` sets for a declaration the resolver can't weigh, which a rule that
reaches the footer link may not hold."""

LENGTH_UNITS = frozenset(("px", "rem", "em"))
"""The units the resolver takes in a padding, margin or scroll margin."""


def box_word(box: str, word: str) -> bool | None:
    """Whether a browser takes ``word`` as one side of ``box``: a length, a percentage
    anywhere but a scroll margin, ``auto`` in a margin alone, and nothing below zero in a
    padding. ``None`` for a unit outside `LENGTH_UNITS` or a number this can't read."""
    found = re.fullmatch(rf"({SIGNED})([a-z]+|%)?", word)
    if found is None:
        if re.match(r"[+-]?[\d.]", word):
            return None
        return box == "margin" and word == "auto"
    amount, unit = float(found.group(1)), found.group(2)
    if unit is None:
        return amount == 0
    if unit != "%" and unit not in LENGTH_UNITS:
        return None
    return not ((box == "padding" and amount < 0) or (box == "scroll-margin" and unit == "%"))


def dropped_offset(value: str) -> bool:
    """Whether a browser drops ``value`` as an outline's offset, which takes one length of
    either sign: a word other than a CSS-wide keyword, such as ``thin``, a percentage, or a
    number other than zero with no unit. Anything else is kept for `pixels` to read or refuse."""
    found = re.fullmatch(rf"({SIGNED})(%?)", value)
    if found is not None:
        return found.group(2) == "%" or float(found.group(1)) != 0
    return re.fullmatch(NAME, value) is not None and value not in KEYWORDS


def link_longhands(name: str, value: str, tokens: Mapping[str, str | None]) -> dict[str, str]:
    """What one declaration sets among `LINK_PROPERTIES`: a padding, margin or scroll
    margin shorthand, or its block form, the top and bottom it reaches, a logical side
    the top or bottom it is in a page written top to bottom, and an outline by
    `outline_parts` with ``tokens``. A box declaration with a side a browser doesn't take,
    by `box_word`, is dropped, as a browser drops it, and one with an escape, a function or
    a side this can't read is refused, and so is an outline style a browser doesn't know.
    An outline offset `dropped_offset` drops is dropped the same way. ``all`` sets
    `REFUSED`; `check_root_text` refuses an escaped name and an animation."""
    if name == "all":
        return {REFUSED: name}
    if name == "outline":
        return outline_parts(value, tokens)
    value = value.lower()
    if name == "outline-width" and outline_width(value) is False:
        return {}
    if name == "outline-offset" and dropped_offset(value):
        return {}
    if name == "outline-style" and value not in OUTLINE_STYLES:
        raise UnreadCss(value)
    words = words_of(value)
    boxed = next((box for box in BOXES if name.startswith(box)), None)
    if boxed and value not in KEYWORDS:
        if "\\" in value or "(" in value:
            raise UnreadCss(value)
        taken = [box_word(boxed, word) for word in words]
        if False in taken:
            return {}
        if None in taken:
            raise UnreadCss(value)
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


def either_visit(chosen: Selector, link: Element, head: str) -> bool:
    """Whether ``chosen`` matches ``link`` whether or not the link was visited. A selector
    that matches it in only one of those states is refused."""
    unvisited = chosen.matches(link)
    if chosen.matches(link, "visited") != unvisited:
        raise UnreadCss(unshielded(head))
    return unvisited


def reach_of(head: str, link: Element) -> tuple[tuple[int, int, int], bool] | None:
    """How one selector, as `style_rules` gives it, reaches ``link``: its specificity, and
    whether only while the link has focus. ``None`` where it can't match the link in any
    state. A selector that would reach it in a state other than focus, or with a state
    anywhere but its end, is refused, and so is one that reaches it only while it is visited,
    or only while it isn't."""
    anyhow = head
    while (bare := re.sub(r":not\([^()]*\)", "", anyhow, flags=re.I)) != anyhow:
        anyhow = bare
    anyhow = STATES.sub("*", anyhow).strip()
    last = compound(unshielded(re.split(r"[\s>+~]+", anyhow)[-1]))
    if not (last.matches(link) or last.matches(link, "visited")):
        return None
    if not STATES.search(head):
        chosen = selector(unshielded(head))
        return (chosen.specificity, False) if either_visit(chosen, link, head) else None
    shape = selector(unshielded(anyhow))
    if not (shape.matches(link) or shape.matches(link, "visited")):
        return None
    focus = ON_FOCUS.fullmatch(head.strip())
    if focus is None or STATES.search(focus["before"]):
        raise UnreadCss(unshielded(head))
    before = focus["before"]
    if not before.strip() or before[-1] in f"{CSS_SPACES}>":
        before += "*"
    chosen = selector(unshielded(before))
    if not either_visit(chosen, link, head):
        return None
    a, b, c = chosen.specificity
    return (a, b + len(re.findall(":focus", focus["states"], re.I)), c), True


NAME = r"(?:-?[a-zA-Z_]|--)[\w-]*"
ELEMENTS = "before|after|first-line|first-letter|marker|placeholder|selection|backdrop"
COMPOUND_SHAPE = re.compile(
    rf"""(?:{NAME}|\*)?
    (?:\#{NAME}|\.{NAME}|\[{NAME}(?:=(?:{NAME}|"[^"]*"|'[^']*'))?\]|:{NAME}(?:\([^()]*\))?)*
    (?P<element>::(?:{ELEMENTS}))?""",
    re.I | re.X,
)
"""One compound selector as a browser parses it: a type or ``*`` first, then ids, classes,
attributes and pseudo-classes named by identifiers, and a pseudo-element a browser knows
last."""


def well_formed(head: str) -> bool:
    """Whether one selector of a list, as `style_rules` gives it, has the shape a browser
    parses: each compound by `COMPOUND_SHAPE`, a pseudo-element in the last one only, and a
    ``:not()`` around a compound with no pseudo-element."""
    parts = re.split(r"\s*>\s*|\s+", head.strip())
    for index, part in enumerate(parts):
        shape = COMPOUND_SHAPE.fullmatch(part)
        if not part or shape is None or (shape["element"] and index < len(parts) - 1):
            return False
        for inner in re.findall(r":not\(([^()]*)\)", part, re.I):
            inside = COMPOUND_SHAPE.fullmatch(inner.strip())
            if not inner.strip() or inside is None or inside["element"]:
                return False
    return True


def list_reach(heads: str, link: Element) -> list[tuple[tuple[int, int, int], bool]]:
    """How each selector of a list, as `style_rules` gives it, reaches ``link``, by `reach_of`.
    A browser drops the whole rule for one selector it can't parse, so where any of them
    reaches the link, a selector `well_formed` or `selector` doesn't take is refused."""
    reaches = [reach_of(head, link) for head in heads.split(",")]
    found = [reach for reach in reaches if reach is not None]
    if found:
        for head in heads.split(","):
            if not well_formed(head):
                raise UnreadCss(unshielded(head))
            selector(unshielded(head))
    return found


def link_rules(
    css: str, link: Element, read: Callable[[str, str], dict[str, str]] | None = None
) -> dict[str, list[LinkRule]]:
    """Every value a rule of ``css`` gives ``link`` among the properties ``read`` takes from
    a declaration, `LINK_PROPERTIES` by default, by property, each declaration of a rule over
    an earlier one unless that one is important. A value the resolver cannot read, a keyword
    or a `SUBSTITUTION`, is refused where its rule reaches ``link``, and so is a selector
    list `list_reach` can't read."""
    tokens = root_tokens(css)
    found: dict[str, list[LinkRule]] = {}
    for style in rules_of(css):
        declared: dict[str, tuple[str, bool]] = {}
        for name, value, important in style.declarations:
            try:
                given = read(name, value) if read else link_longhands(name, value, tokens)
            except UnreadCss as unread:
                given = {REFUSED: str(unread)}
            for longhand, setting in given.items():
                if important or not declared.get(longhand, ("", False))[1]:
                    declared[longhand] = (setting, important)
        if not declared:
            continue
        reaches = list_reach(style.selectors, link)
        if reaches and REFUSED in declared:
            raise UnreadCss(declared[REFUSED][0])
        for reach in reaches:
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
    found = re.fullmatch(rf"([+-]?(?:{NUMBER.pattern}))(px|rem)?", OUTLINE_WIDTHS.get(value, value))
    if found is None or (found.group(2) is None and float(found.group(1)) != 0):
        raise UnreadCss(value)
    return float(found.group(1)) * (view.root_text if found.group(2) == "rem" else 1)


def views_for(*found: dict[str, list[LinkRule]]) -> list[View]:
    """`FOOTER_VIEWS`, and the widths each length named by a media query around a rule in
    ``found`` comes to at either text size, rounded down, and one pixel past that, so a view
    falls in every window between two of them that holds a whole pixel."""
    widths: set[int] = set()
    for rule in (one for rules in found for each in rules.values() for one in each):
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
SCALES = frozenset(("transform", "scale"))
BOX_SIZES = frozenset(("height", "max-height", "block-size", "max-block-size"))
LEFT_AS_IS = frozenset(("none", "auto"))
FIRST_LINE = re.compile(r"::?first-line(?![\w-])", re.I)
TEXT_NAMES = frozenset(("all", "font", "font-size", "line-height"))


def check_root_text(css: str, link: Element) -> None:
    """Refuse a rule that sets the root element's text size, which the views give and every
    `rem` is read by, that zooms, scales or transforms the link or an element above it, that
    sets the link's height, that sets the text size or line height of the first line of
    either, or that reaches either with an escaped property name or an animation, which may
    stand for any of these."""
    styles = rules_of(css)
    for one in (link, *link.ancestors()):
        names = {"zoom", "font", "font-size"} if one.tag == "html" else {"zoom"}
        sized = SCALES | BOX_SIZES if one is link else SCALES
        for style in styles:
            heads = style.selectors.split(",")
            if any(
                name in names
                or (name in sized and value.strip().lower() not in LEFT_AS_IS)
                or "\\" in name
                or (name in ANIMATION and value != "none")
                for name, value, _ in style.declarations
            ) and any(reach_of(head, one) is not None for head in heads):
                raise UnreadCss(style.selectors)
            lines = [FIRST_LINE.sub("", head) for head in heads if FIRST_LINE.search(head)]
            if any(name in TEXT_NAMES or "\\" in name for name, _, _ in style.declarations) and any(
                reach_of(head, one) is not None for head in lines
            ):
                raise UnreadCss(style.selectors)


TEXT_PROPERTIES = ("font-size", "line-height")
INHERITED = chr(0) + "inherit"
"""What `text_longhands` sets for ``inherit`` or ``unset``, which take the parent's value
for the text size and the line height. It opens with a NUL, which `plain_ascii` refuses in
any sheet `style_rules` reads, so no value a rule writes is taken for it."""


def text_longhands(name: str, value: str) -> dict[str, str]:
    """What one declaration sets of the text size and the line height: `INHERITED` for
    ``inherit`` or ``unset``, nothing for a negative value, which a browser drops, and
    `REFUSED` for ``all`` and any other ``font`` shorthand, which may set either. A value with
    a `SUBSTITUTION` or an escape is kept as written, for `link_rules` to refuse, since a
    browser keeps a declaration with a substitution until the page is styled."""
    value = value.strip().lower()
    if name == "all":
        return {REFUSED: name}
    if name == "font":
        return (
            dict.fromkeys(TEXT_PROPERTIES, INHERITED)
            if value in ("inherit", "unset")
            else {REFUSED: name}
        )
    if name not in TEXT_PROPERTIES:
        return {}
    if value in ("inherit", "unset"):
        return {name: INHERITED}
    if SUBSTITUTION.search(value) or "\\" in value:
        return {name: value}
    return {} if negative(value) else {name: value.removeprefix("-")}


def text_pixels(value: str, size: float, view: View) -> float:
    """A text size or a line height in CSS pixels on ``view``: pixels, a `rem` of the root's
    text size, or an `em` or a percent of ``size``. Any other value is refused."""
    found = re.fullmatch(rf"({NUMBER.pattern})(px|rem|em|%)?", value)
    if found is None or (found.group(2) is None and float(found.group(1)) != 0):
        raise UnreadCss(value)
    amount = float(found.group(1))
    return {"rem": amount * view.root_text, "em": amount * size, "%": amount * size / 100}.get(
        found.group(2) or "px", amount
    )


def line_pixels(texts: list[dict[str, list[LinkRule]]], view: View, *, focused: bool) -> float:
    """The footer link's line height in CSS pixels on ``view``, inherited down ``texts``, the
    text rules of each element from the root to the link: a number times the link's own text
    size, or a length as it comes to where it is set. Only the link's own rules for focus
    hold while it has focus. ``normal`` depends on the font and is refused."""
    size: float = view.root_text
    line, scaled = None, False
    for depth, rules in enumerate(texts):
        mine = focused and depth == len(texts) - 1
        given = link_value(rules, "font-size", view, focused=mine)
        if depth and given is not None and given != INHERITED:
            size = text_pixels(given, size, view)
        given = link_value(rules, "line-height", view, focused=mine)
        if given == "normal":
            line = None
        elif given is not None and given != INHERITED:
            scaled = NUMBER.fullmatch(given) is not None
            line = float(given) if scaled else text_pixels(given, size, view)
    if line is None:
        msg = f"the footer link's line height is the font's own on {view}"
        raise UnreadCss(msg)
    return line * size if scaled else line


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
    inline block padded at least 0.8rem above and below, so the padding grows its line,
    and at least 44 pixels tall with the line height it inherits; no rule takes the press
    area back with a margin; and its margin and the room a scroll to it keeps are at least
    as deep above and below as its outline reaches, and the outline is drawn in a color that
    shows."""
    check_comments(css)
    for link in footer_links():
        check_root_text(css, link)
        rules = link_rules(css, link)
        texts = [link_rules(css, one, text_longhands) for one in [link, *link.ancestors()][::-1]]
        taken = [
            rule.value
            for side in SIDES
            for rule in rules.get(f"margin-{side}", [])
            if pixels(rule.value, FOOTER_VIEWS[0]) < 0
        ]
        assert not taken, f"a rule margins the footer link's press area back in: {taken}"
        for view in views_for(rules, *texts):
            style = link_value(rules, "outline-style", view, focused=True)
            if style is None or style == "auto":
                msg = f"the browser's own focus outline on {view}"
                raise UnreadCss(msg)
            assert style not in ("none", "hidden"), f"the footer link shows no outline on {view}"
            color = link_value(rules, "outline-color", view, focused=True) or "currentcolor"
            assert visible(color), f"the footer link's outline can't be seen on {view}"
            width = link_value(rules, "outline-width", view, focused=True) or "medium"
            offset = link_value(rules, "outline-offset", view, focused=True) or "0"
            assert pixels(width, view) > 0, f"the footer link's outline has no width on {view}"
            reach = pixels(width, view) + pixels(offset, view)
            for focused in (False, True):
                seen = f"{view}, {'with focus' if focused else 'at rest'}"
                shown = link_value(rules, "display", view, focused=focused)
                assert shown == "inline-block", f"the footer link is {shown} on {seen}"
                tall = line_pixels(texts, view, focused=focused) + sum(
                    pixels(link_value(rules, f"padding-{side}", view, focused=focused) or "0", view)
                    for side in SIDES
                )
                assert tall >= 44, f"the footer link is {tall:.2f} pixels tall on {seen}"
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


def outline_list(member: str) -> str:
    """The footer link's outline taken away, then drawn again by a list that also holds
    ``member``."""
    return (
        ".colophon a:focus-visible { outline: none; }\n"
        f".colophon a:focus-visible, {member} {{ outline: 3px solid red; }}"
    )


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
    "an-outline-width-calculated-to-nothing": ".colophon a:focus-visible { "
    "outline: calc(0px) solid red; }",
    "an-outline-width-calculated-too-wide": ".colophon a:focus-visible { "
    "outline: calc(10px) solid red; }",
    "an-outline-width-of-the-smaller": ".colophon a:focus-visible { "
    "outline: min(3px, 10px) solid red; }",
    "a-calculation-in-capitals": ".colophon a:focus-visible { outline: CALC(0px) solid red; }",
    "a-calculation-where-the-color-goes": ".colophon a:focus-visible { "
    "outline: 3px solid calc(1px); }",
    "a-function-inside-a-color": ".colophon a:focus-visible { "
    "outline: 3px solid color-mix(in srgb, rgb(0 0 0), red); }",
    "an-outline-color-never-set": ".colophon a:focus-visible { "
    "outline: 3px solid var(--undefined-footer-outline); }",
    "an-outline-color-named-in-another-case": ".colophon a:focus-visible { "
    "outline: 3px solid var(--Blue-action); }",
    "an-outline-color-set-on-some-screens": "@media (max-width: 30rem) { :root { --ring: red; } }\n"
    ".colophon a:focus-visible { outline: 3px solid var(--ring); }",
    "an-outline-color-set-on-the-link": ".colophon a { --ring: red; }\n"
    ".colophon a:focus-visible { outline: 3px solid var(--ring); }",
    "an-outline-color-set-on-the-root-element-by-name": "html { --ring: red; }\n"
    ".colophon a:focus-visible { outline: 3px solid var(--ring); }",
    "an-outline-color-that-is-a-width": ":root { --blue-action: 10px; }",
    "an-outline-color-that-is-a-keyword": ":root { --ring: inherit; }\n"
    ".colophon a:focus-visible { outline: 3px solid var(--ring); }",
    "an-important-token-before-a-later-one": ":root { --blue-action: 10px !important; }\n"
    ":root { --blue-action: red; }",
    "a-fallback-that-is-a-width": ".colophon a:focus-visible { "
    "outline: 3px solid var(--unset-ring, 10px); }",
    "a-whole-outline-too-wide": ":root { --ring: 4px solid red; }\n"
    ".colophon a:focus-visible { outline: var(--ring); }",
    "a-token-from-another-token": ":root { --ring: var(--blue-action); }\n"
    ".colophon a:focus-visible { outline: 3px solid var(--ring); }",
    "a-fallback-from-another-token": ".colophon a:focus-visible { "
    "outline: 3px solid var(--unset-ring, var(--blue-action)); }",
    "an-environment-value": ".colophon a:focus-visible { "
    "outline: env(safe-area-inset-top) solid red; }",
    "a-focus-rule-for-a-link-without-an-address": ".colophon a:focus-visible { margin: 0; }\n"
    ".colophon a:not([href]):focus-visible { margin: 5px 0; }",
    "a-focus-rule-for-no-link": ".colophon a:focus { margin: 0; }\n"
    ".colophon a:not(a):focus { margin: 5px 0; }",
    "no-line-to-the-link": "footer.colophon a { line-height: 0; }",
    "no-line-above": "footer { line-height: 0; }",
    "a-line-just-short": ".colophon a { padding: 1rem 0; line-height: 11.9px; }",
    "text-too-small-for-the-line": ".colophon a { font-size: 0.1rem; }",
    "a-line-in-ems-of-the-footer": ".colophon { line-height: 1em; }\n"
    ".colophon a { font-size: 3rem; }",
    "a-line-only-with-focus": ".colophon a:focus-visible { line-height: 0; }",
    "a-line-on-narrow-screens": "@media (max-width: 30rem) { .colophon a { line-height: 0; } }",
    "a-line-left-to-the-font": ".colophon a { line-height: normal; }",
    "the-font-shorthand": ".colophon a { font: 8px/0 sans-serif; }",
    "a-line-from-a-custom-property": ".colophon a { line-height: var(--line); }",
    "a-calculated-line": ".colophon a { line-height: calc(0px); }",
    "a-text-size-keyword": ".colophon a { font-size: smaller; }",
    "a-set-height": ".colophon a { height: 20px; }",
    "a-largest-block-size": ".colophon a { max-block-size: 1rem; }",
    "a-scaled-link": ".colophon a { transform: scale(0.5); }",
    "a-scaled-footer": "footer { scale: 0.5; }",
    "a-short-line-in-a-media-window": "@media (min-width: 600px) and (max-width: 601px) { "
    "footer { line-height: 0; } }",
    "text-in-ems-too-small": ".colophon p { font-size: 0.1em; }",
    "text-in-percent-too-small": ".colophon p { font-size: 10%; }",
    "a-line-in-rem-just-short": ".colophon a { padding: 1rem 0; line-height: 0.74rem; }",
    "everything-reset-above": "footer { all: initial; }",
    "a-first-line-with-no-height": ".colophon a::first-line { line-height: 0; }",
    "a-first-line-above": ".colophon p:first-line { font-size: 0.1rem; }",
    "a-word-that-is-no-color": ".colophon a:focus-visible { outline: none; }\n"
    ".colophon a:focus-visible { outline: 3px solid banana; }",
    "a-calculation-glued-to-a-color": ".colophon a:focus-visible { "
    "outline: solid rgb(0 0 0)calc(0px); }",
    "a-line-height-with-a-trailing-point": ".colophon a { line-height: 0; }\n"
    ".colophon a { line-height: 2.; }",
    "a-margin-with-a-trailing-point": ".colophon a { margin: 0; scroll-margin: 0; }\n"
    ".colophon a { margin: 5.px 0; scroll-margin: 5.px 0; }",
    "a-line-of-minus-zero": ".colophon a { line-height: -0; }",
    "an-outline-of-minus-zero": ".colophon a:focus-visible { outline: -0 solid red; }",
    "an-outline-width-of-minus-zero": ".colophon a:focus-visible { outline-width: -0px; }",
    "a-margin-in-exponent-notation": ".colophon a { margin: 0e0px 0; scroll-margin: 0e0px 0; }",
    "two-numbers-run-together": ".colophon a { margin: 0.0.5px 0; }",
    "an-escaped-name-on-the-first-line": ".colophon a::first-line { line-h\\65ight: 0; }",
    "a-hex-color-of-five-digits": ".colophon a:focus-visible { outline: 3px solid #12345; }",
    "the-footer-text-halved": ".colophon { font-size: 50%; }",
    "no-line-to-this-link": ".colophon a { line-height: 0; }",
    "a-clear-outline": ".colophon a:focus-visible { outline-color: transparent; }",
    "a-clear-outline-in-capitals": ".colophon a:focus-visible { outline-color: TRANSPARENT; }",
    "a-clear-outline-in-the-shorthand": ".colophon a:focus-visible { "
    "outline: 3px solid transparent; }",
    "a-clear-hex-of-four-digits": ".colophon a:focus-visible { outline: 3px solid #4c70; }",
    "a-clear-hex-of-eight-digits": ".colophon a:focus-visible { outline-color: #4c719300; }",
    "a-clear-rgba": ".colophon a:focus-visible { outline-color: rgba(76, 113, 147, 0); }",
    "a-clear-rgb-in-percent": ".colophon a:focus-visible { outline-color: rgb(76 113 147 / 0%); }",
    "a-clear-hsl": ".colophon a:focus-visible { outline: 3px solid hsl(210 32% 44% / 0); }",
    "an-alpha-of-none": ".colophon a:focus-visible { outline-color: rgb(76 113 147 / none); }",
    "a-negative-alpha": ".colophon a:focus-visible { outline-color: rgb(76 113 147 / -0.5); }",
    "a-clear-token": ":root { --ring: transparent; }\n"
    ".colophon a:focus-visible { outline: 3px solid var(--ring); }",
    "the-action-color-cleared": ":root { --blue-action: transparent; }",
    "a-clear-color-held-at-rest": ".colophon a { outline-color: transparent !important; }",
    "a-clear-color-as-strong-as-the-focus-rule": ".colophon a { outline-color: transparent; }",
    "the-text-color-for-an-outline": ".colophon a:focus-visible { outline-color: currentcolor; }",
    "an-outline-with-no-color": ".colophon a:focus-visible { outline: 3px solid; }",
    "an-empty-fallback": ".colophon a:focus-visible { outline: 3px solid var(--unset-ring,); }",
    "a-mixed-color": ".colophon a:focus-visible { "
    "outline: 3px solid color-mix(in srgb, transparent, transparent); }",
    "a-color-by-the-scheme": ".colophon a:focus-visible { "
    "outline: 3px solid light-dark(transparent, red); }",
    "a-color-a-browser-cant-show": ".colophon a:focus-visible { outline-color: invert; }",
    "a-color-made-from-a-clear-one": ".colophon a:focus-visible { "
    "outline-color: rgb(from transparent r g b); }",
    "a-color-made-from-a-clear-one-in-the-shorthand": ".colophon a:focus-visible { "
    "outline: 3px solid rgb(from #0000 r g b); }",
    "a-color-made-from-a-clear-one-in-a-token": ":root { "
    "--blue-action: hsl(from #4c719300 h s l); }",
    "a-clear-color-once-visited": ".colophon a:visited { outline-color: transparent; }",
    "a-clear-color-once-visited-with-focus": ".colophon a:visited:focus-visible { "
    "outline-color: transparent; }",
    "a-clear-color-unless-unvisited": ".colophon a:not(:link) { outline-color: transparent; }",
    "a-clear-color-unless-unvisited-with-focus": ".colophon a:not(:link):focus-visible { "
    "outline-color: transparent; }",
    "a-word-that-is-no-color-in-a-weaker-rule": "footer a { outline: 3px solid banana; }",
    "an-undefined-width": ".colophon a:focus-visible { "
    "outline: var(--undefined-footer-width) solid red; }",
    "a-token-set-elsewhere-before-longhands": ".card { --ring: 1px dotted red; }\n"
    ".colophon a:focus-visible { outline: var(--ring); outline-style: solid; "
    "outline-color: red; }",
    "a-clear-token-over-a-fallback": ":root { --ring: transparent; }\n"
    ".colophon a:focus-visible { outline: 3px solid var(--ring, red); }",
    "an-escaped-number": ".colophon a { margin: \\30 ; scroll-margin: \\30 ; }",
    "a-clear-color-unless-not-visited": ".colophon a:not(:not(:visited)) { "
    "outline-color: transparent; }",
    "a-color-function-a-browser-drops": ".colophon a:focus-visible { outline: none; "
    "outline: 3px solid rgb(banana); }",
    "a-color-function-a-browser-drops-with-an-alpha": ".colophon a:focus-visible { "
    "outline: none; outline: 3px solid rgb(banana / 1); }",
    "a-legacy-color-function-a-browser-drops": ".colophon a:focus-visible { outline: none; "
    "outline: 3px solid rgba(banana, banana, banana, 1); }",
    "a-color-function-a-browser-drops-in-capitals": ".colophon a:focus-visible { "
    "outline: none; outline: 3px solid RGB(BANANA); }",
    "an-outline-color-a-browser-drops": ".colophon a:focus-visible { "
    "outline-color: transparent; outline-color: rgb(banana); }",
    "a-token-a-browser-cant-read": ":root { --blue-action: rgb(banana); }",
    "a-width-from-an-outline-a-browser-drops": ".colophon a:focus-visible { "
    "outline: 8px solid red; outline: 1px solid rgb(banana); outline-color: red; }",
    "a-width-from-an-outline-with-a-mixed-color": ".colophon a:focus-visible { "
    "outline: 8px solid red; outline: 1px solid color-mix(banana); outline-color: red; }",
    "an-outline-style-a-browser-drops": ".colophon a:focus-visible { outline-style: none; "
    "outline-style: banana; }",
    "padding-a-browser-drops-for-auto": ".colophon a { padding: 0; padding: 0.8rem auto; }",
    "padding-a-browser-drops-for-a-negative-side": ".colophon a { padding: 0; "
    "padding: 0.8rem -1px; }",
    "padding-a-browser-drops-for-a-side-just-below-zero": ".colophon a { padding: 0; "
    "padding: 0.8rem -0.01px; }",
    "padding-a-browser-drops-for-auto-on-the-left": ".colophon a { padding: 0; "
    "padding: 0.8rem 0 0.8rem auto; }",
    "padding-a-browser-drops-in-capitals": ".colophon a { padding: 0; PADDING: 0.8REM AUTO; }",
    "a-scroll-margin-a-browser-drops-for-auto": ".colophon a { scroll-margin: 0; "
    "scroll-margin: 5px auto; }",
    "a-scroll-margin-a-browser-drops-for-a-percentage": ".colophon a { scroll-margin: 0; "
    "scroll-margin: 5px 5%; }",
    "a-margin-a-browser-drops-for-a-plain-number": ".colophon a { margin: 0; margin: 5px 8; }",
    "a-margin-a-browser-drops-for-a-plain-number-on-the-left": ".colophon a { margin: 0; "
    "margin: 5px 0 5px 8; }",
    "a-margin-a-browser-drops-for-an-unknown-unit": ".colophon a { margin: 0; "
    "margin: 5px 1banana; }",
    "a-unit-at-the-sides-the-resolver-doesnt-read": ".colophon a { margin: 5px 1vw; }",
    "a-calculation-at-the-sides": ".colophon a { margin: 5px calc(1px); }",
    "an-escape-at-the-sides": ".colophon a { margin: 5px 0\\70x; }",
    "a-legacy-color-function-a-browser-drops-for-one-channel": ".colophon a:focus-visible { "
    "outline: none; outline: 3px solid rgba(banana,0,0,1); }",
    "a-width-from-an-outline-with-an-alpha-a-browser-drops": ".colophon a:focus-visible { "
    "outline: 8px solid red; outline: 1px solid rgb(0 0 0 / 1px); outline-color: red; }",
    "a-function-glued-to-a-width-after-a-color": ".colophon a:focus-visible { outline: none; "
    "outline: rgb(0 0 0) 3calc(1px) solid; outline-width: 3px; }",
    "an-escaped-keyword-at-the-sides": ".colophon a { margin: 0 \\61uto; }",
    "a-width-from-a-legacy-outline-with-an-alpha-of-none": ".colophon a:focus-visible { "
    "outline: 8px solid red; outline: 1px solid rgba(0, 0, 0, none); outline-color: red; }",
    "a-calculation-nested-in-a-side": ".colophon a { padding: calc(0px + (0px)) 0; }",
    "a-fallback-with-a-calculation-in-a-margin": ".colophon a { "
    "margin: var(--none-set, calc(0px)) 0; }",
    "nested-functions-in-a-scroll-margin": ".colophon a { "
    "scroll-margin: min(0px, max(0px, 1px)) 0; }",
    "an-outline-with-a-plain-number-width-then-a-width": ".colophon a:focus-visible { "
    "outline: none; outline: 1 solid red; outline-width: 3px; }",
    "an-outline-with-a-width-just-over-zero-then-a-width": ".colophon a:focus-visible { "
    "outline: none; outline: 0.01 solid red; outline-width: 3px; }",
    "an-outline-with-an-unknown-width-unit-then-a-width": ".colophon a:focus-visible { "
    "outline: none; outline: 3banana solid red; outline-width: 3px; }",
    "an-outline-with-an-unknown-width-unit-in-capitals": ".colophon a:focus-visible { "
    "outline: none; OUTLINE: 3BANANA SOLID RED; outline-width: 3px; }",
    "an-outline-with-a-percentage-width-then-a-width": ".colophon a:focus-visible { "
    "outline: none; outline: 10% solid red; outline-width: 3px; }",
    "an-outline-with-a-negative-width-then-a-width": ".colophon a:focus-visible { "
    "outline: none; outline: -1px solid red; outline-width: 3px; }",
    "an-outline-with-a-width-unit-the-resolver-doesnt-read": ".colophon a:focus-visible { "
    "outline: none; outline: 1vw solid red; outline-width: 3px; }",
    "an-outline-with-an-unreadable-width-number": ".colophon a:focus-visible { "
    "outline: none; outline: 1e1px solid red; outline-width: 3px; }",
    "an-outline-with-a-plain-number-width-from-a-token": ":root { --w: 1; }\n"
    ".colophon a:focus-visible { outline: var(--w) solid red; outline-width: 3px; }",
    "an-outline-with-a-plain-number-width-in-its-own-rule": ".colophon a:focus-visible { "
    "outline: none; }\n.colophon a:focus-visible { outline: 1 solid red; }\n"
    ".colophon a:focus-visible { outline-width: 3px; }",
    "an-unknown-outline-width-unit": ".colophon a:focus-visible { outline-width: 3banana; }",
    "an-outline-with-an-unknown-width-unit": ".colophon a:focus-visible { "
    "outline: 3banana solid red; }",
    "a-number-glued-to-a-unit-by-a-substitution": ":root { --w: 3; }\n"
    ".colophon a:focus-visible { outline: var(--w)px solid red; }",
    "a-number-glued-to-a-unit-from-two-substitutions": ":root { --n: 3; --u: px; }\n"
    ".colophon a:focus-visible { outline: var(--n)var(--u) solid red; }",
    "a-number-glued-to-a-unit-by-a-fallback": ".colophon a:focus-visible { "
    "outline: var(--unset-width, 3)px solid red; }",
    "a-color-glued-to-text-by-a-substitution": ":root { --c: re; }\n"
    ".colophon a:focus-visible { outline: 3px solid var(--c)d; }",
    "a-point-glued-before-a-substitution": ":root { --w: 5px; }\n"
    ".colophon a:focus-visible { outline: .var(--w) solid red; }",
    "a-channel-glued-to-a-substitution": ":root { --r: 7; }\n"
    ".colophon a:focus-visible { outline: 3px solid rgb(var(--r)6 113 147); }",
    "a-style-glued-before-a-substitution": ":root { --w: 3px; --c: red; }\n"
    ".colophon a:focus-visible { outline: var(--w) solidvar(--c); }",
    "a-hex-glued-before-a-substitution": ":root { --w: 3px; --s: solid; }\n"
    ".colophon a:focus-visible { outline: var(--w) #abcvar(--s); }",
    "a-width-glued-before-a-substitution": ":root { --c: red; --s: solid; }\n"
    ".colophon a:focus-visible { outline: var(--c) 3pxvar(--s); }",
    "a-channel-glued-before-a-substitution": ":root { --w: 3px solid; --a: / 1; }\n"
    ".colophon a:focus-visible { outline: var(--w) rgb(76 113 147var(--a)); }",
    "a-style-glued-before-a-fallback": ":root { --w: 3px; }\n"
    ".colophon a:focus-visible { outline: var(--w) solidvar(--unset-color, red); }",
    "an-outline-color-token-left-empty": ":root { --blue-action: ; }",
    "a-whole-outline-token-left-empty-over-its-fallback": ":root { --ring: ; }\n"
    ".colophon a:focus-visible { outline: var(--ring, 3px solid red); }",
    "a-whole-outline-token-left-empty-with-no-space": ":root { --ring:; }\n"
    ".colophon a:focus-visible { outline: var(--ring, 3px solid red); }",
    "an-outline-color-token-left-empty-with-no-space": ":root { --blue-action:; }",
    "an-outline-color-token-left-empty-and-important": ":root { --blue-action: !important; }",
    "an-outline-color-token-left-empty-but-for-a-comment": ":root { --blue-action: /* - */ ; }",
    "an-outline-color-token-left-empty-at-the-end-of-its-rule": ":root { --blue-action: }",
    "an-outline-color-token-left-empty-at-the-end-of-the-sheet": ":root { --blue-action:",
    "an-escaped-token-name-left-empty-over-a-fallback": ":root { --r\\69ng: ; }\n"
    ".colophon a:focus-visible { outline: var(--ring, 3px solid red); }",
    "a-token-name-with-escaped-dashes-left-empty-over-a-fallback": ":root { \\2d-ring: ; }\n"
    ".colophon a:focus-visible { outline: var(--ring, 3px solid red); }",
    "a-token-name-with-an-escaped-dash-left-empty-on-some-screens": "@media (min-width: 1px) { "
    ":root { -\\-ring: ; } }\n.colophon a:focus-visible { outline: var(--ring, 3px solid red); }",
    "an-outline-width-token-left-empty-then-set-wider": ":root { --w: ; }\n"
    ":root { --w: 10px; }\n.colophon a:focus-visible { outline: var(--w) solid red; }",
    "an-outline-color-token-left-empty-on-some-screens": "@media (min-width: 1px) { "
    ":root { --blue-action: ; } }",
    "an-outline-color-token-left-empty-where-supported": "@supports (overflow-wrap: anywhere) { "
    ":root { --blue-action: ; } }",
    "an-outline-color-token-left-empty-above-the-link": ".colophon { --blue-action: ; }",
    "a-line-height-a-browser-drops-over-a-short-one": ".colophon a { line-height: 0; "
    "line-height: inherited; }",
    "a-text-size-a-browser-drops-over-a-small-one": ".colophon a { font-size: 0; "
    "font-size: inherited; }",
    "a-line-height-a-browser-drops-in-capitals": ".colophon a { line-height: 0; "
    "line-height: INHERITED; }",
    "a-line-height-a-browser-drops-after-a-minus": ".colophon a { line-height: 0; "
    "line-height: -inherited; }",
    "a-line-height-a-browser-drops-made-important": ".colophon a { line-height: 0; }\n"
    ".colophon a { line-height: inherited !important; }",
    "a-line-height-a-browser-drops-above-the-link": ".colophon { line-height: 0; "
    "line-height: inherited; }",
    "a-line-height-of-a-word-a-browser-drops": ".colophon a { line-height: inherited; }",
    "a-text-size-of-a-word-a-browser-drops": ".colophon a { font-size: inherited; }",
    "a-line-height-written-as-the-inherit-marker": ".colophon a { line-height: 0; "
    f"line-height: {INHERITED}; }}",
    "a-list-with-a-member-a-browser-drops": ".colophon a:focus-visible { outline: none; }\n"
    ".colophon a:focus-visible, span > > span { outline: 3px solid red; }",
    "a-list-opening-on-a-member-a-browser-drops": ".colophon a:focus-visible { outline: none; }\n"
    "span > > span, .colophon a:focus-visible { outline: 3px solid red; }",
    "a-list-with-a-member-opening-on-a-child-combinator": ".colophon a:focus-visible { "
    "outline: none; }\n.colophon a:focus-visible, > span { outline: 3px solid red; }",
    "a-list-with-a-member-ending-on-a-child-combinator": ".colophon a:focus-visible { "
    "outline: none; }\n.colophon a:focus-visible, span > { outline: 3px solid red; }",
    "a-list-with-an-empty-member": ".colophon a:focus-visible { outline: none; }\n"
    ".colophon a:focus-visible, { outline: 3px solid red; }",
    "a-list-with-an-unknown-state-in-a-member": ".colophon a:focus-visible { outline: none; }\n"
    ".colophon a:focus-visible, p:bogus span { outline: 3px solid red; }",
    "a-list-with-a-child-selector-in-a-not": ".colophon a:focus-visible { outline: none; }\n"
    ".colophon a:focus-visible, span:not(> span) { outline: 3px solid red; }",
    "a-list-with-a-member-the-resolver-cannot-read": ".colophon a:focus-visible { "
    "outline: none; }\n.colophon a:focus-visible, h1 + p { outline: 3px solid red; }",
    "a-list-a-browser-drops-for-the-padding": ".colophon a { padding: 0; }\n"
    ".colophon a, span > > span { padding: 0.8rem 0; }",
    "a-list-a-browser-drops-for-the-margin": ".colophon a { margin: 0; }\n"
    ".colophon a, span > > span { margin: 5px 0; }",
    "a-list-a-browser-drops-for-the-scroll-margin": ".colophon a { scroll-margin: 0; }\n"
    ".colophon a, span > > span { scroll-margin: 5px 0; }",
    "a-list-a-browser-drops-for-the-display": ".colophon a { display: inline; }\n"
    ".colophon a, span > > span { display: inline-block; }",
    "a-list-a-browser-drops-for-the-line-height": ".colophon a { line-height: 0; }\n"
    ".colophon a, span > > span { line-height: 3rem; }",
    "a-list-a-browser-drops-for-the-text-size": ".colophon a { font-size: 4px; }\n"
    ".colophon a, span > > span { font-size: 1rem; }",
    "a-list-a-browser-drops-above-the-link": ".colophon p { line-height: 0; }\n"
    ".colophon p, span > > span { line-height: 3rem; }",
    "a-list-a-browser-drops-in-a-media-rule": ".colophon a:focus-visible { outline: none; }\n"
    "@media screen { .colophon a:focus-visible, span > > span { outline: 3px solid red; } }",
    "a-list-a-browser-drops-in-capitals": ".colophon a:focus-visible { outline: none; }\n"
    ".colophon a:focus-visible, SPAN > > SPAN { outline: 3px solid red; }",
    "a-list-a-browser-drops-over-several-lines": ".colophon a:focus-visible { outline: none; }"
    "\n.colophon a:focus-visible,\n  span > > span {\n  outline: 3px solid red;\n}",
    "a-list-a-browser-drops-made-important": ".colophon a { padding: 0; }\n"
    ".colophon a, span > > span { padding: 0.8rem 0 !important; }",
    "a-line-height-with-a-substitution-after-a-minus": ".colophon { line-height: 0; }\n"
    ".colophon a { line-height: 3rem; line-height: -1 var(--missing); }",
    "a-text-size-with-a-substitution-after-a-minus": ".colophon { font-size: 4px; }\n"
    ".colophon a { font-size: 1rem; font-size: -1 var(--missing); }",
    "a-line-height-with-an-environment-value-after-a-minus": ".colophon { line-height: 0; }\n"
    ".colophon a { line-height: 3rem; line-height: -1 env(--missing); }",
    "a-line-height-with-an-attribute-after-a-minus": ".colophon { line-height: 0; }\n"
    ".colophon a { line-height: 3rem; line-height: -1 attr(data-line); }",
    "a-line-height-with-a-condition-after-a-minus": ".colophon { line-height: 0; }\n"
    ".colophon a { line-height: 3rem; line-height: -1 if(media(print): 1); }",
    "a-line-height-with-a-dashed-function-after-a-minus": ".colophon { line-height: 0; }\n"
    ".colophon a { line-height: 3rem; line-height: -1 --scale(2); }",
    "a-line-height-with-an-escaped-substitution-after-a-minus": ".colophon { line-height: 0; }"
    "\n.colophon a { line-height: 3rem; line-height: -1 \\76 ar(--missing); }",
    "a-line-height-with-an-escaped-unit-after-a-minus": ".colophon a { line-height: 3rem; "
    "line-height: -1\\70 x; }",
    "a-line-height-with-a-substitution-in-capitals": ".colophon { line-height: 0; }\n"
    ".colophon a { line-height: 3rem; line-height: -1 VAR(--missing); }",
    "a-line-height-with-a-substitution-made-important": ".colophon { line-height: 0; }\n"
    ".colophon a { line-height: -1 var(--missing) !important; line-height: 3rem; }",
    "a-line-height-with-a-substitution-above-the-link": ".colophon { line-height: 0; }\n"
    ".colophon p { line-height: 3rem; line-height: -1 var(--missing); }",
    "a-line-height-with-a-substitution-on-focus": ".colophon p { line-height: 0; }\n"
    ".colophon a { line-height: 3rem; }\n"
    ".colophon a:focus-visible { line-height: -1 var(--missing); }",
    "a-line-height-with-a-substitution-in-a-media-rule": ".colophon { line-height: 0; }\n"
    "@media screen { .colophon a { line-height: 3rem; line-height: -1 var(--missing); } }",
    "a-list-with-a-class-named-by-a-number": outline_list(".1"),
    "a-list-with-an-id-named-by-a-number": outline_list("#1"),
    "a-list-with-an-attribute-named-by-a-number": outline_list("[1a]"),
    "a-list-with-a-class-named-by-a-hyphen": outline_list(".-"),
    "a-list-with-a-pseudo-element-before-a-descendant": outline_list("span::before span"),
    "a-list-with-a-pseudo-element-before-a-class": outline_list("a::before.x"),
    "a-list-with-two-pseudo-elements": outline_list("a::before::after"),
    "a-list-with-a-pseudo-element-in-a-not": outline_list("p:not(::before)"),
    "a-list-with-an-old-style-pseudo-element-before-a-child": outline_list("span:before > span"),
    "a-list-with-an-unknown-pseudo-element": outline_list("span::bogus"),
    "a-list-with-an-empty-not": outline_list("p:not()"),
    "a-list-with-a-type-after-a-star": outline_list("*a"),
    "a-list-with-a-star-after-a-type": outline_list("a*"),
    "a-list-with-a-type-after-an-attribute": outline_list("[href]a"),
    "a-list-with-a-pseudo-element-before-a-descendant-for-the-line-height": ".colophon p { "
    "line-height: 0; }\n.colophon p, span::before span { line-height: 3rem; }",
    "a-rule-with-a-type-after-an-attribute": ".colophon a:focus-visible { outline: none; }\n"
    ".colophon [href]a:focus-visible { outline: 3px solid red; }",
    "an-outline-offset-a-browser-drops-for-thin": ".colophon a:focus-visible { "
    "outline-offset: 10px; outline-offset: thin; }",
    "an-outline-offset-a-browser-drops-for-medium": ".colophon a { margin: 6px 0; "
    "scroll-margin: 6px 0; }\n"
    ".colophon a:focus-visible { outline-offset: 10px; outline-offset: medium; }",
    "an-outline-offset-a-browser-drops-for-thick": ".colophon a { margin: 8px 0; "
    "scroll-margin: 8px 0; }\n"
    ".colophon a:focus-visible { outline-offset: 10px; outline-offset: thick; }",
    "an-outline-offset-a-browser-drops-in-a-later-rule": ".colophon a:focus-visible { "
    "outline-offset: 10px; }\n.colophon a:focus-visible { outline-offset: thin; }",
    "an-outline-offset-a-browser-drops-made-important": ".colophon a:focus-visible { "
    "outline-offset: 10px; }\n.colophon a:focus-visible { outline-offset: thin !important; }",
    "an-outline-offset-a-browser-drops-in-capitals": ".colophon a:focus-visible { "
    "outline-offset: 10px; OUTLINE-OFFSET: THIN; }",
    "an-outline-offset-a-browser-drops-at-rest": ".colophon.colophon a { outline-offset: 10px; "
    "outline-offset: thin; }",
    "an-outline-offset-a-browser-drops-in-a-media-rule": "@media (max-width: 30rem) { "
    ".colophon a:focus-visible { outline-offset: 10px; outline-offset: thin; } }",
    "an-outline-offset-a-browser-drops-for-a-word": ".colophon a:focus-visible { "
    "outline-offset: 10px; outline-offset: auto; }",
    "an-outline-offset-a-browser-drops-for-a-percentage": ".colophon a:focus-visible { "
    "outline-offset: 10px; outline-offset: 0%; }",
    "an-outline-offset-a-browser-drops-for-a-number-without-a-unit": ".colophon "
    "a:focus-visible { outline-offset: 10px; outline-offset: 0.5; }",
    "an-outline-offset-with-an-escaped-unit": ".colophon a:focus-visible { "
    "outline-offset: 2px; outline-offset: 9p\\78; }",
    "an-outline-offset-keyword": ".colophon a:focus-visible { outline-offset: 2px; "
    "outline-offset: inherit; }",
    "an-outline-offset-keyword-in-capitals": ".colophon a:focus-visible { "
    "outline-offset: 2px; outline-offset: INHERIT; }",
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
    "an-outline-a-browser-drops-for-two-colors": ".colophon a:focus-visible { "
    "outline: 6px solid red red; }",
    "an-outline-a-browser-drops-for-two-widths": ".colophon a:focus-visible { "
    "outline: 6px 6px solid; }",
    "an-outline-a-browser-drops-for-two-styles": ".colophon a:focus-visible { "
    "outline: 6px solid dotted; }",
    "a-color-function": ".colophon a:focus-visible { outline: 3px solid rgb(76 113 147); }",
    "a-hex-color": ".colophon a:focus-visible { outline: 3px solid #4c7193; }",
    "an-outline-color-with-a-fallback": ".colophon a:focus-visible { "
    "outline: 3px solid var(--unset-ring, red); }",
    "a-whole-outline-from-the-root": ":root { --ring: 3px solid red; }\n"
    ".colophon a:focus-visible { outline: var(--ring); }",
    "a-later-token": ":root { --blue-action: 10px; }\n:root { --blue-action: red; }",
    "a-token-on-the-root-in-capitals": ":ROOT { --ring: red; }\n"
    ".colophon a:focus-visible { outline: 3px solid var(--ring); }",
    "a-substitution-in-capitals": ".colophon a:focus-visible { "
    "outline: 3px solid VAR(--blue-action); }",
    "spaces-inside-a-substitution": ".colophon a:focus-visible { "
    "outline: 3px solid var( --blue-action ); }",
    "a-focus-rule-for-this-link-by-what-it-is-not": ".colophon a:focus-visible { margin: 0; }\n"
    ".colophon a:not(.other):focus-visible { margin: 5px 0; }",
    "a-line-at-the-value": ".colophon a { padding: 1rem 0; line-height: 12px; }",
    "a-line-just-over": ".colophon a { padding: 1rem 0; line-height: 12.1px; }",
    "a-taller-line-above": "footer { line-height: 2; }",
    "a-line-in-numbers-of-the-link-text": ".colophon { line-height: 1; }\n"
    ".colophon a { font-size: 2rem; }",
    "the-font-inherited": ".colophon a { font: inherit; }",
    "the-line-inherited": ".colophon a { line-height: inherit; font-size: unset; }",
    "a-text-size-in-percent": ".colophon a { font-size: 100%; }",
    "a-height-left-to-its-content": ".colophon a { height: auto; max-height: none; "
    "block-size: auto; max-block-size: none; }",
    "no-transform": ".colophon a { transform: none; scale: none; }",
    "a-negative-line-dropped": ".colophon a { line-height: -1; }",
    "a-short-line-elsewhere": ".places a { line-height: 0; }",
    "the-font-line-overridden-below": "footer { line-height: normal; }\n"
    ".colophon a { line-height: 2; }",
    "an-outline-from-three-tokens": ":root { --w: 3px; --s: solid; --c: red; }\n"
    ".colophon a:focus-visible { outline: var(--w) var(--s) var(--c); }",
    "a-focus-rule-for-the-footer-itself": "footer:focus-visible { line-height: 0; }",
    "a-token-for-another-element": ".card { --ring: red; }\n"
    ".card a:focus-visible { outline: 3px solid var(--ring); }",
    "a-keyword-outline-for-another-element": ".card a:focus-visible { outline: inherit; }",
    "a-first-line-elsewhere": ".places p::first-line { line-height: 0; }",
    "a-first-line-color": ".colophon a::first-line { color: red; }",
    "a-text-size-in-ems-above": ".colophon p { font-size: 2em; }",
    "a-line-in-ems-of-large-footer-text": ".colophon { font-size: 2rem; line-height: 1em; }\n"
    ".colophon a { font-size: 0.5rem; }",
    "the-line-inherited-over-a-short-one": ".colophon a { line-height: 0; }\n"
    ".colophon a { line-height: inherit; }",
    "an-outline-color-of-its-own": ".colophon a:focus-visible { outline-color: red; }",
    "an-outline-color-in-capitals": ".colophon a:focus-visible { outline-color: RED; }",
    "a-hex-with-full-alpha": ".colophon a:focus-visible { outline-color: #4c7193ff; }",
    "a-hex-of-four-digits-just-seen": ".colophon a:focus-visible { outline: 3px solid #4c71; }",
    "an-alpha-just-over-none": ".colophon a:focus-visible { "
    "outline-color: rgb(76 113 147 / 0.01); }",
    "an-alpha-in-a-legacy-rgba": ".colophon a:focus-visible { "
    "outline-color: rgba(76, 113, 147, 1); }",
    "an-alpha-in-percent": ".colophon a:focus-visible { "
    "outline: 3px solid hsl(210 32% 44% / 50%); }",
    "a-clear-color-weaker-than-the-focus-rule": "footer a { outline-color: transparent; }",
    "a-clear-color-elsewhere": ".places a:focus-visible { outline-color: transparent; }",
    "a-clear-color-for-another-visited-link": ".places a:visited { outline-color: transparent; }",
    "a-visited-footer-which-is-no-link": ".colophon:visited a { outline-color: transparent; }",
    "an-outline-a-browser-drops-for-a-keyword": ".colophon a:focus-visible { "
    "outline: 3px solid inherit; }",
    "padding-a-browser-drops-for-auto": ".colophon a { padding: 0 auto; }",
    "a-negative-padding-dropped": ".colophon a { padding: -1px 0; }",
    "a-block-padding-a-browser-drops": ".colophon a { padding-block: 0 auto; }",
    "a-top-padding-a-browser-drops": ".colophon a { padding-top: auto; }",
    "a-scroll-margin-a-browser-drops-for-a-percentage": ".colophon a { scroll-margin: 0 5%; }",
    "a-margin-a-browser-drops-for-a-plain-number": ".colophon a { margin: 0 8; }",
    "a-margin-with-auto-at-the-sides": ".colophon a { margin: 0; margin: 5px auto; }",
    "a-margin-with-a-percentage-below-zero-at-the-sides": ".colophon a { margin: 0; "
    "margin: 5px -5%; }",
    "a-margin-in-ems-at-the-sides": ".colophon a { margin: 0; margin: 5px 1em; }",
    "padding-with-a-percentage-at-the-sides": ".colophon a { padding: 0; padding: 0.8rem 5%; }",
    "padding-in-rem-at-the-sides": ".colophon a { padding: 0; padding: 0.8rem 1rem; }",
    "padding-of-minus-zero-at-the-sides": ".colophon a { padding: 0; padding: 0.8rem -0; }",
    "a-scroll-margin-below-zero-at-the-sides": ".colophon a { scroll-margin: 0; "
    "scroll-margin: 5px -1px; }",
    "an-outline-after-none-in-a-color-function": ".colophon a:focus-visible { outline: none; "
    "outline: 3px solid rgb(0 0 0); }",
    "an-outline-after-none-in-a-legacy-color-function": ".colophon a:focus-visible { "
    "outline: none; outline: 3px solid rgba(0,0,0,1); }",
    "padding-with-a-pixel-side": ".colophon a { padding: 0; padding: 0.8rem 1px; }",
    "a-scroll-margin-with-zero-sides": ".colophon a { scroll-margin: 0; scroll-margin: 5px 0; }",
    "a-margin-with-pixel-sides": ".colophon a { margin: 0; margin: 5px 8px; }",
    "an-outline-with-a-zero-width-then-a-width": ".colophon a:focus-visible { "
    "outline: none; outline: 0 solid red; outline-width: 3px; }",
    "an-outline-with-a-zero-pixel-width-then-a-width": ".colophon a:focus-visible { "
    "outline: none; outline: 0px solid red; outline-width: 3px; }",
    "an-outline-with-a-pixel-width-then-a-width": ".colophon a:focus-visible { "
    "outline: none; outline: 1px solid red; outline-width: 3px; }",
    "an-outline-with-its-own-width-then-a-width": ".colophon a:focus-visible { "
    "outline: none; outline: 3px solid red; outline-width: 3px; }",
    "an-outline-with-a-width-in-ems-then-a-width": ".colophon a:focus-visible { "
    "outline: none; outline: 1em solid red; outline-width: 3px; }",
    "an-outline-with-a-width-in-rem": ".colophon a:focus-visible { "
    "outline: none; outline: 0.05rem solid red; }",
    "an-outline-with-a-width-keyword-in-capitals": ".colophon a:focus-visible { "
    "outline: none; outline: THIN solid red; }",
    "an-outline-with-a-plain-number-width-dropped": ".colophon a:focus-visible { "
    "outline: 1 solid red; }",
    "an-outline-with-an-unknown-width-unit-for-another-link": ".places a:focus-visible { "
    "outline: 3banana solid red; }",
    "a-plain-number-outline-width-dropped": ".colophon a:focus-visible { outline-width: 1; }",
    "an-outline-width-just-over-zero-dropped": ".colophon a:focus-visible { outline-width: 0.01; }",
    "a-percentage-outline-width-dropped": ".colophon a:focus-visible { outline-width: 10%; }",
    "a-plain-number-outline-width-before-a-width": ".colophon a:focus-visible { "
    "outline-width: 3px; outline-width: 1; }",
    "a-substitution-beside-a-space": ":root { --w: 3px; }\n"
    ".colophon a:focus-visible { outline: var(--w) solid red; }",
    "a-substitution-just-inside-a-color-function": ":root { --c: 76 113 147; }\n"
    ".colophon a:focus-visible { outline: 3px solid rgb(var(--c)); }",
    "a-substitution-beside-commas": ":root { --r: 76; }\n"
    ".colophon a:focus-visible { outline: 3px solid rgb(var(--r),113,147); }",
    "a-substitution-beside-a-slash": ":root { --c: 76 113 147; }\n"
    ".colophon a:focus-visible { outline: 3px solid rgb(var(--c)/1); }",
    "an-empty-token-the-link-does-not-use": ":root { --unused-ring: ; }",
    "an-empty-token-in-other-capitals": ":root { --Blue-Action: ; }",
    "an-empty-token-for-another-link": ".card { --ring: ; }\n"
    ".card a:focus-visible { outline: var(--ring, 3px solid red); }",
    "an-empty-token-written-in-a-string": '.card::after { content: "a;--blue-action:;b"; }',
    "an-empty-token-written-in-a-comment": ".card { color: red; /* ;--blue-action: ; */ }",
    "an-empty-token-in-keyframes": "@keyframes fade { to { --blue-action: ; } }",
    "an-empty-token-in-a-font-face": "@font-face { font-family: x; --blue-action: ; }",
    "a-token-name-with-no-colon": ":root { --blue-action }",
    "a-token-name-with-no-colon-over-a-fallback": ":root { --ring }\n"
    ".colophon a:focus-visible { outline: var(--ring, 3px solid red); }",
    "a-whole-outline-token-set-over-its-fallback": ":root { --ring: 3px solid red; }\n"
    ".colophon a:focus-visible { outline: var(--ring, 3px solid red); }",
    "a-list-with-a-member-for-another-element": ".colophon a:focus-visible { outline: none; }\n"
    ".colophon a:focus-visible, span > span { outline: 3px solid red; }",
    "a-list-opening-on-a-member-for-another-element": ".colophon a:focus-visible { "
    "outline: none; }\nspan > span, .colophon a:focus-visible { outline: 3px solid red; }",
    "a-list-with-a-state-in-another-member": ".colophon a:focus-visible { outline: none; }\n"
    ".card:hover, .colophon a:focus-visible { outline: 3px solid red; }",
    "a-list-with-a-not-in-another-member": ".colophon a:focus-visible { outline: none; }\n"
    "p:not(.card) span, .colophon a:focus-visible { outline: 3px solid red; }",
    "a-list-with-a-pseudo-element-in-another-member": ".colophon a:focus-visible { "
    "outline: none; }\nspan::before, .colophon a:focus-visible { outline: 3px solid red; }",
    "a-list-with-a-comma-in-a-string-in-another-member": ".colophon a:focus-visible { "
    'outline: none; }\n[title="a,b"] span, .colophon a:focus-visible { outline: 3px solid red; }',
    "a-list-over-several-lines": ".colophon a:focus-visible { outline: none; }\n"
    ".colophon a:focus-visible,\n  span > span {\n  outline: 3px solid red;\n}",
    "a-list-a-browser-drops-for-other-elements": "span > > span, .card a { padding: 0; "
    "margin: 0; display: inline; }",
    "a-line-height-with-a-substitution-then-a-length": ".colophon { line-height: 0; }\n"
    ".colophon a { line-height: -1 var(--missing); line-height: 3rem; }",
    "a-negative-line-height-alone-over-a-length": ".colophon { line-height: 0; }\n"
    ".colophon a { line-height: 3rem; line-height: -1; }",
    "a-negative-line-height-glued-to-a-substitution": ".colophon { line-height: 0; }\n"
    ".colophon a { line-height: 3rem; line-height: -1var(--missing); }",
    "a-substitution-after-a-minus-for-another-element": ".card { line-height: -1 var(--m); }",
    "a-list-with-a-star-and-a-class-in-another-member": outline_list("*.card span"),
    "a-list-with-a-state-in-a-not-in-another-member": outline_list("p:not(:hover) span"),
    "a-list-with-a-class-opening-on-a-hyphen-in-another-member": outline_list(".-x span"),
    "a-list-with-a-known-pseudo-element-in-another-member": outline_list("input::placeholder"),
    "a-list-whose-second-member-weighs-more": ".colophon a:focus-visible { outline: none; }\n"
    "a, .colophon a:focus-visible { outline: 3px solid red; }",
    "an-outline-offset-a-browser-drops-over-the-value-for-medium": ".colophon "
    "a:focus-visible { outline-offset: 2px; outline-offset: medium; }",
    "an-outline-offset-a-browser-drops-over-the-value-for-thick": ".colophon "
    "a:focus-visible { outline-offset: 2px; outline-offset: thick; }",
    "an-outline-offset-a-browser-drops-alone-in-a-later-rule": ".colophon a:focus-visible { "
    "outline-offset: medium; }",
    "an-outline-offset-a-browser-drops-made-important-over-the-value": ".colophon "
    "a:focus-visible { outline-offset: medium !important; }",
    "an-outline-offset-a-browser-drops-in-capitals-over-the-value": ".colophon "
    "a:focus-visible { outline-offset: 2px; OUTLINE-OFFSET: MEDIUM; }",
    "an-outline-offset-a-browser-drops-with-spaces-over-the-value": ".colophon "
    "a:focus-visible { outline-offset: 2px; outline-offset :  medium  ; }",
    "an-outline-offset-a-browser-drops-for-a-word-over-the-value": ".colophon "
    "a:focus-visible { outline-offset: 2px; outline-offset: auto; }",
    "an-outline-offset-a-browser-drops-for-a-percentage-over-the-value": ".colophon "
    "a:focus-visible { outline-offset: 2px; outline-offset: 10%; }",
    "an-outline-offset-a-browser-drops-for-a-zero-percentage-over-the-value": ".colophon "
    "a:focus-visible { outline-offset: 2px; outline-offset: 0%; }",
    "an-outline-offset-a-browser-drops-for-one-without-a-unit": ".colophon "
    "a:focus-visible { outline-offset: 2px; outline-offset: 1; }",
    "an-outline-offset-a-browser-drops-for-a-half-without-a-unit": ".colophon "
    "a:focus-visible { outline-offset: 2px; outline-offset: .5; }",
    "an-outline-offset-a-browser-drops-for-minus-one-without-a-unit": ".colophon "
    "a:focus-visible { outline-offset: 2px; outline-offset: -1; }",
    "an-outline-offset-of-zero-without-a-unit": ".colophon a:focus-visible { "
    "outline-offset: 10px; outline-offset: 0; }",
    "an-outline-offset-of-minus-zero-without-a-unit": ".colophon a:focus-visible { "
    "outline-offset: 10px; outline-offset: -0; }",
    "a-negative-outline-offset": ".colophon a:focus-visible { outline-offset: 10px; "
    "outline-offset: -2px; }",
    "an-outline-offset-over-a-wider-one": ".colophon a:focus-visible { outline-offset: 10px; "
    "outline-offset: 2px; }",
    "a-thin-outline-width": ".colophon a:focus-visible { outline-width: thin; }",
    "a-medium-outline-width-in-the-shorthand": ".colophon a:focus-visible { "
    "outline: medium solid red; }",
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


def test_only_custom_properties_left_empty_are_found() -> None:
    css = ":root { color: ; --Ring: ; --w:; --v }\n.card { --c: red; --d: !important; }"
    assert empty_tokens(css) == {"--Ring", "--w", "--d"}
    for escaped in (":root { --r\\69ng: ; }", ":root { \\2d-ring: ; }"):
        with pytest.raises(UnreadCss):
            empty_tokens(escaped)


COLORS_SHOWN = {
    "rgb(76 113 147)": True,
    "rgb(76 113 147 / 0.5)": True,
    "rgb(76 113 147/50%)": True,
    "rgb(76, 113, 147)": True,
    "rgba(76,113,147,0.5)": True,
    "rgba(76 113 147 / 1)": True,
    "rgb(30% 40 50)": True,
    "rgb(30%, 40%, 50%)": True,
    "rgb(none 0 0)": True,
    "rgb(-10 +10 .5)": True,
    "rgb( 76 113 147 )": True,
    "RGB(76 113 147)": True,
    "hsl(210deg 32% 44%)": True,
    "hsl(210, 32%, 44%)": True,
    "hsla(1turn, 32%, 44%, 50%)": True,
    "hsl(210 32 44)": True,
    "hsl(none 32% 44%)": True,
    "hwb(210 10% 20% / 0.5)": True,
    "lab(50% 20 30)": True,
    "lch(50 20% 30deg)": True,
    "oklab(0.5 0.1 0.1)": True,
    "oklch(0.5 0.1 none)": True,
    "color(srgb 1 0 0 / 0.5)": True,
    "color(xyz-d65 0.1 0.2 0.3)": True,
    "color(display-p3 100% 0% none)": True,
    "rgb(0 0 0 / 0)": False,
    "rgba(76, 113, 147, 0)": False,
    "hsl(210, 32%, 44%, 0%)": False,
    "color(srgb 1 0 0 / -0.5)": False,
}
"""Colors a browser takes, and whether an outline drawn in each shows."""

COLORS_REFUSED = [
    "rgb(banana)",
    "rgb(banana / 1)",
    "rgba(banana, banana, banana, 1)",
    "rgb(30%, 40, 50)",
    "rgb(none, 0, 0)",
    "rgb(10deg 0 0)",
    "rgb(0 0)",
    "rgb(0 0 0 0)",
    "rgb(0 0 0 / 1 / 1)",
    "rgb(0,0 0)",
    "rgb(0 0 0,)",
    "rgb()",
    "rgb(0 0 0 / 1px)",
    "rgb(0, 0, 0, none)",
    "rgb(0, 0, 0 / 1)",
    "rgb(76 113 147 /)",
    "rgb(5. 0 0)",
    "hsl(210, 32, 44)",
    "hsl(210px 32% 44%)",
    "hsl(32% 32% 44%)",
    "hsl(none, 32%, 44%)",
    "hwb(210, 10%, 20%)",
    "lab(50, 20, 30)",
    "lab(50deg 20 30)",
    "lch(50 20 30%)",
    "oklch(0.5 0.1 30%)",
    "color(banana 1 0 0)",
    "color(srgb 1 0)",
    "color(srgb, 1, 0, 0)",
    "color(1 0 0)",
    "color(srgb 1deg 0 0)",
    "rgb(1e2 0 0)",
    "rgb(0 0 0 / none)",
    "color(display-p3-linear 1 0 0)",
]
"""Colors a browser drops, and a few it takes that the resolver doesn't read: a number in
exponent notation, an opacity of ``none``, and a color space outside the common ones."""


@pytest.mark.parametrize(("color", "shown"), COLORS_SHOWN.items(), ids=COLORS_SHOWN.keys())
def test_a_color_a_browser_takes_is_read(color: str, shown: bool) -> None:
    assert visible(color) is shown


@pytest.mark.parametrize("color", COLORS_REFUSED)
def test_a_color_a_browser_drops_is_refused(color: str) -> None:
    with pytest.raises(UnreadCss):
        visible(color)
