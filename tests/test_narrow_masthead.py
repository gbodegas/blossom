# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Gerardo Bodegas Martinez
"""The masthead and the family page's Add assignments forms on a narrow screen with large
text, worked out from the stylesheet and the pages as rendered.

At 320 pixels with text at 200%, the wordmark, the page links, and the sign-out control
are wider together than the line, and an entry column of 13rem is wider than the card
that holds it. The page links wrap onto as many lines as they need at every width; where
the masthead is a column, the brand stays within its line and the wordmark goes under
the mark when the two do not fit it; the entry
columns are never wider than the form; and the fold's summary and a label or button in
Add assignments break a word only when that word cannot fit its line. On a screen where
everything fits, each of these leaves the layout as it is.

Elsewhere on the pages, at the same sizes: every heading, each note inside one of the family
page's folds, and the example of school text break a word only when it is wider than its
line; a form that asks the family for a decision is one column as wide as the form, so a
field's own width never widens it and a long word in its button breaks; the side padding of
a decision's buttons, of her Ask a parent for help in Help, and of a problem line in Help is
the rule that gives way on a phone with text at 200%, as measured in Edge, while the sign-in
button and the button that asks about a note keep theirs; and the skip link's focus outline
stays on the screen.

No browser runs in these tests, so the cascade is resolved here, on the page and
stylesheet reading in `tests/support.py`, for the part of CSS these elements depend on:
compound selectors of a type, an id, classes, attributes and pseudo-classes, with
descendant and child combinators, in comma lists; importance, then specificity, then
source order; and inheritance for the properties a browser inherits. A rule inside a media
query counts only where the query holds, and one inside a `@supports` block only in a
browser that passes its test, Edge unless a view says otherwise. A media query reads its
`rem` from the browser's own text size, so large text is checked two ways: the browser's
text size doubled, which moves the narrow layout's breakpoint with it, and the page's own
text doubled, which leaves the breakpoint where it is. A media feature or `@supports` test
the resolver does not know is refused, never assumed either way.
"""

import functools
import itertools
import math
import pathlib
import re
import sys
from collections.abc import Callable
from dataclasses import dataclass, field, replace
from html import escape

import pytest

from blossom.routes.navigation import note_help_href
from blossom.settings import REPOSITORY_ROOT
from tests.support import (
    ESSAY_ID,
    HER_PAGE,
    KEYWORDS,
    PAGE_HEADERS,
    STAND_INS,
    SUBSTITUTION,
    Element,
    Rule,
    Selector,
    UnreadCss,
    View,
    compound,
    elements_of,
    form_fields,
    household_client,
    media_length,
    one_query_holds,
    selector,
    sign_in_as,
    state_of,
    style_rules,
    unshielded,
    waiting_note,
    winner,
)
from tests.support import holds as condition_holds

WIDTHS = (320, 820, 1180, 1440, 3840)
"""The viewport widths, in CSS pixels, the pages are held to: a phone, a tablet upright and
on its side, a laptop, and a large desktop screen."""

TEXT_SIZES = {
    "ordinary text": (16, 16),
    "the browser's text at 200%": (32, 32),
    "the page's text at 200%": (16, 32),
}
"""Each way of reading, as the browser's own text size (what a media query's `rem` is
measured in) and the root element's text size (what a property's `rem` is measured in)."""

INHERITED = frozenset(
    {
        "overflow-wrap",
        "white-space",
        "text-wrap",
        "text-wrap-mode",
        "font-size",
        "letter-spacing",
        "word-spacing",
    }
)


VIEWS = [View(width, browser, root) for width in WIDTHS for browser, root in TEXT_SIZES.values()]
ROW_VIEWS = [*VIEWS, View(481, 16, 32), View(540, 16, 32), View(600, 16, 32), View(961, 32, 32)]
"""Every view, and the widths just above the narrow layout, where the masthead is a row
again and its line is shortest."""


# ------------------------------------------------------------- the pages, as elements


def masthead_of(page: str) -> list[Element]:
    """The masthead and everything in it."""
    found = elements_of(page)
    heads = [one for one in found if one.tag == "header" and "masthead" in one.classes]
    assert len(heads) == 1
    return [one for one in found if one.within(heads[0])]


def adding_of(page: str) -> list[Element]:
    """The two forms of the family page's Add assignments fold and everything in them."""
    found = elements_of(page)
    folds = [one for one in found if one.attributes.get("id") == "add-assignments"]
    assert len(folds) == 1
    forms = [one for one in found if one.tag == "form" and fold_holds(folds[0], one)]
    assert [form.attributes["action"] for form in forms] == [
        "/parent/inbox/read",
        "/parent/inbox/enter",
    ]
    return [one for one in found if any(one.within(form) for form in forms)]


def entry_surface_of(page: str) -> list[Element]:
    """The summary that opens the Add assignments fold, then its two forms and everything
    in them."""
    found = elements_of(page)
    folds = [one for one in found if one.attributes.get("id") == "add-assignments"]
    assert [fold.tag for fold in folds] == ["details"]
    summaries = [one for one in found if one.tag == "summary" and one.parent is folds[0]]
    assert len(summaries) == 1
    return summaries + adding_of(page)


def fold_holds(fold: Element, one: Element) -> bool:
    return any(above is fold for above in one.ancestors())


# ------------------------------------------------------------- the stylesheet, as rules


WATCHED = frozenset(
    {
        "align-items",
        "align-self",
        "appearance",
        "box-sizing",
        "display",
        "flex-direction",
        "flex-shrink",
        "flex-wrap",
        "grid-template-columns",
        "overflow",
        "overflow-wrap",
        "overflow-x",
        "text-overflow",
        "white-space",
        "font-size",
        "width",
        "min-width",
        "max-width",
        "padding-left",
        "padding-right",
        "border-left-width",
        "border-right-width",
        "border-top-width",
        "border-bottom-width",
        "margin-left",
        "margin-right",
        "background",
        "background-color",
        "background-image",
        "box-shadow",
        "border-top-left-radius",
        "border-top-right-radius",
        "border-bottom-right-radius",
        "border-bottom-left-radius",
        "backdrop-filter",
        "column-count",
        "column-gap",
        "letter-spacing",
        "word-spacing",
        "zoom",
        "text-wrap",
        "text-wrap-mode",
    }
)
"""The properties these checks read. A rule that sets none of them is passed over unread."""


@dataclass
class Sheet:
    rules: dict[str, list[Rule]] = field(default_factory=dict)
    """Each watched property's rules, in source order."""
    matched: dict[tuple[Element, str], list[Rule]] = field(default_factory=dict)
    """The rules that reach each element, by property, whatever the screen. Keyed on the
    element itself, which keeps it alive while the sheet is in use."""


LOGICAL = {"inline-size": "width", "min-inline-size": "min-width", "max-inline-size": "max-width"}
CORNERS = (
    "border-top-left-radius",
    "border-top-right-radius",
    "border-bottom-right-radius",
    "border-bottom-left-radius",
)
"""Each logical width and the width it sets in a page written left to right."""
STYLES = frozenset(
    {"none", "hidden", "solid", "dashed", "dotted", "double", "groove", "ridge", "inset", "outset"}
)
ONE_KEYWORD = {
    "flex-wrap": frozenset({"nowrap", "wrap", "wrap-reverse"}),
    "flex-direction": frozenset({"row", "row-reverse", "column", "column-reverse"}),
    "box-sizing": frozenset({"content-box", "border-box"}),
    "overflow-wrap": frozenset({"normal", "break-word", "anywhere"}),
}
"""The watched properties whose every value is one keyword, each with the keywords it takes
beside `KEYWORDS`."""


def words_of(value: str) -> list[str]:
    """The words of a value, a function such as ``min(1rem, 2vw)`` kept whole."""
    return re.findall(r"(?:[^\s(]|\([^()]*\))+", value)


def sides(value: str) -> tuple[str, str, str, str]:
    """The top, right, bottom and left of a box shorthand of one to four values."""
    words = words_of(value)
    if not 1 <= len(words) <= 4:
        raise UnreadCss(value)
    right = words[1] if len(words) > 1 else words[0]
    bottom = words[2] if len(words) > 2 else words[0]
    return words[0], right, bottom, (words[3] if len(words) == 4 else right)


def zero(value: str | None) -> bool:
    """Whether a length or number is zero, in any of the ways a browser reads one."""
    return value is not None and bool(
        re.fullmatch(r"[+-]?(0+\.?0*|\.0+)(px|rem|em|%|vw|vh)?", value)
    )


def border_width(value: str) -> str:
    """The width a ``border`` shorthand gives a side: none at all when its style is none."""
    width, style = "medium", "none"
    for word in words_of(value):
        if word in STYLES:
            style = word
        elif word in ("thin", "medium", "thick") or re.match(
            r"[\d.+-]|(min|max|calc|clamp)\(", word
        ):
            width = word
    return "0" if style in ("none", "hidden") else width


def longhands(name: str, value: str) -> dict[str, str]:
    """What one declaration sets among the watched properties: ``font`` sets the text size,
    ``word-wrap`` is another name for ``overflow-wrap``, ``all`` is refused,
    ``flex-flow`` the direction and the wrapping, ``flex`` the shrinking, a logical width
    its width, ``place-items`` or ``place-self`` the alignment across a column with its
    first word, ``gap`` the space between items on a line with its last, and a padding,
    border or margin shorthand each side it reaches. A property of one keyword given any
    other value sets nothing, as a browser drops it, and so does a ``flex-flow`` with two
    directions or two wrappings; a `SUBSTITUTION` or an escape there is refused, since it
    may stand for a keyword."""
    taken = ONE_KEYWORD.get("overflow-wrap" if name == "word-wrap" else name)
    if taken is not None and value not in taken | KEYWORDS:
        if SUBSTITUTION.search(value) or "\\" in value:
            raise UnreadCss(value)
        return {}
    if name == "font":
        return {"font-size": value}
    if name == "word-wrap":
        return {"overflow-wrap": value}
    if name == "all":
        raise UnreadCss(value)
    if name in LOGICAL:
        return {LOGICAL[name]: value}
    if name in ("place-items", "place-self"):
        return {name.replace("place", "align"): value.split()[0]}
    if name == "flex-flow":
        found = {"flex-direction": "row", "flex-wrap": "nowrap"}
        given: set[str] = set()
        for word in value.split():
            if word in ("wrap", "nowrap", "wrap-reverse"):
                longhand = "flex-wrap"
            elif word in ("row", "row-reverse", "column", "column-reverse"):
                longhand = "flex-direction"
            else:
                raise UnreadCss(value)
            if longhand in given:
                return {}
            given.add(longhand)
            found[longhand] = word
        return found
    if name == "flex":
        numbers = re.findall(r"(?:^|\s)([+-]?(?:\d+\.?\d*|\.\d+))(?=\s|$)", value)
        if value in KEYWORDS:
            return {"flex-shrink": value}
        if value == "none":
            return {"flex-shrink": "0"}
        return {"flex-shrink": numbers[1] if len(numbers) > 1 else "1"}
    if name == "background":
        if value in KEYWORDS or value == "none":
            fill = "transparent" if value == "none" else value
            return {"background": value, "background-color": fill, "background-image": value}
        return {"background": value}
    if name == "border-radius":
        return dict.fromkeys(CORNERS, "0" if all(zero(one) for one in value.split()) else value)
    if name == "columns":
        return {"column-count": value}
    if name == "gap":
        pair = words_of(value)
        return {"column-gap": pair[-1] if 1 <= len(pair) <= 2 else value}
    name = name.replace("inline-start", "left").replace("inline-end", "right")
    if name in ("padding", "margin", "border-width"):
        top, right, bottom, left = sides(value)
        found = {"top": top, "right": right, "bottom": bottom, "left": left}
    elif name in ("padding-inline", "margin-inline", "border-inline-width"):
        pair = words_of(value)
        if not 1 <= len(pair) <= 2:
            raise UnreadCss(value)
        found = {"left": pair[0], "right": pair[-1]}
    elif name in ("border", "border-inline"):
        width = value if value in KEYWORDS else border_width(value)
        found = dict.fromkeys(("left", "right"), width)
        if name == "border":
            found |= dict.fromkeys(("top", "bottom"), width)
    elif name in ("border-left", "border-right", "border-top", "border-bottom"):
        found = {name.removeprefix("border-"): value if value in KEYWORDS else border_width(value)}
    elif name == "border-style":
        raise UnreadCss(value)
    else:
        return {name: value} if name in WATCHED else {}
    box = name.removesuffix("-width").removesuffix("-inline")
    box = "border" if box.startswith("border") else box
    width = "-width" if box == "border" else ""
    if box == "border" and any(one in ("initial", "unset") for one in found.values()):
        raise UnreadCss(value)
    return {
        f"{box}-{side}{width}": one
        for side, one in found.items()
        if box == "border" or side in ("left", "right")
    }


def read_sheet(css: str) -> Sheet:
    """Every rule of ``css`` that sets a watched property, in source order, each with the
    media condition it sits under, as `tests/support.py` reads the sheet. Names and keywords
    are read in any case, as a browser reads them."""
    sheet = Sheet()
    for style in style_rules(css):
        declared: dict[str, tuple[str, bool]] = {}
        for name, value, important in style.declarations:
            for longhand, setting in longhands(name, value.lower()).items():
                if important or not declared.get(longhand, ("", False))[1]:
                    declared[longhand] = (setting, important)
        if not declared:
            continue
        chosen = [selector(unshielded(head)) for head in style.selectors.split(",")]
        for name, (value, weight) in declared.items():
            for one in chosen:
                sheet.rules.setdefault(name, []).append(
                    Rule(one, value, style.order, style.media, weight)
                )
    return sheet


def declared_for(sheet: Sheet, element: Element, name: str, view: View) -> Rule | None:
    """The rule whose value for ``name`` the cascade settles on for ``element`` on ``view``,
    the element's own or, for an inherited property, the nearest ancestor's."""
    key = (element, name)
    if key not in sheet.matched:
        sheet.matched[key] = [
            rule for rule in sheet.rules.get(name, []) if rule.chosen.matches(element)
        ]
    found = winner(sheet.matched[key], view)
    if found is None and name in INHERITED and element.parent is not None:
        return declared_for(sheet, element.parent, name, view)
    return found


def value_of(sheet: Sheet, element: Element, name: str, view: View) -> str | None:
    """The value the cascade settles on, ``None`` where it is the property's initial value.
    ``inherit``, and ``unset`` on an inherited property, take the parent's value."""
    rule = declared_for(sheet, element, name, view)
    if rule is None:
        return None
    if rule.value == "inherit" or (rule.value == "unset" and name in INHERITED):
        return None if element.parent is None else value_of(sheet, element.parent, name, view)
    if rule.value in ("initial", "unset"):
        return None
    if rule.value in ("revert", "revert-layer"):
        raise UnreadCss(rule.value)
    return rule.value


def stylesheet() -> str:
    return (REPOSITORY_ROOT / "blossom" / "static" / "blossom.css").read_text(encoding="utf-8")


# ------------------------------------------------------------- what each rule must do


def links_wrap(sheet: Sheet, page: str, view: View) -> bool:
    """The page links and the sign-out control are each a whole item of a flex line that
    wraps and stays within the masthead's line, so a control that does not fit goes to a
    line of its own. In a row masthead the links shrink beside the brand as far as their
    widest control; in a column they are sized the way the brand is."""
    head = masthead_of(page)
    nav = [one for one in head if one.tag == "nav" and "places" in one.classes]
    assert len(nav) == 1
    margins = [value_of(sheet, nav[0], side, view) for side in ("margin-left", "margin-right")]
    if (
        value_of(sheet, nav[0], "display", view) not in ("flex", "inline-flex")
        or value_of(sheet, nav[0], "flex-wrap", view) != "wrap"
        or any(inset_px(one, view) != 0 for one in margins)
    ):
        return False
    if not column(sheet, head[0], view):
        least = value_of(sheet, nav[0], "min-width", view)
        shrinks = not zero(value_of(sheet, nav[0], "flex-shrink", view)) and (
            least in (None, "auto", "min-content") or zero(least)
        )
        wraps = value_of(sheet, head[0], "flex-wrap", view) == "wrap"
        return shrinks and (wraps or row_fits(sheet, head[0], view))
    return keeps_within_the_line(sizing_of(sheet, head[0], nav[0], view))


ROW_PX, ROW_EM = 48, 14.71
"""How wide a masthead row is at its narrowest: the brand on one line, the masthead's gap,
and the page links with each control on a line of its own, as ``ROW_PX`` pixels (the mark)
plus ``ROW_EM`` ems of the root's text. Edge lays out the widest, a parent's links, at
283.34, 401 and 518.69 px with 16, 24 and 32 px text. They are measured from the stylesheet's
gaps, link padding and type, so a change to those needs a new measurement."""


def masthead_line(sheet: Sheet, head: Element, view: View) -> float:
    """The masthead's line on ``view``: the screen less its margins, held to its widest, less
    its borders and padding."""
    width = view.width - sum(
        inset_px(value_of(sheet, head, side, view), view) for side in INSETS[:2]
    )
    return capped(sheet, head, width, view) - sum(
        (border_px if side.startswith("border") else padding_px)(
            value_of(sheet, head, side, view), view
        )
        for side in INSETS[2:]
    )


def row_fits(sheet: Sheet, head: Element, view: View) -> bool:
    """Whether the brand and the page links at their narrowest fit one masthead row on
    ``view``, which a masthead that doesn't wrap needs."""
    line = masthead_line(sheet, head, view)
    return round(ROW_PX + ROW_EM * view.root_text, 6) <= round(line, 6)


WORDMARK = "blossom"
WORDMARK_EM = 3.978
"""How wide the wordmark's letters are, in ems of its text, before its letter spacing, which
a browser adds after every letter. Edge draws it at 117.25, 175.86 and 234.48 px in 28, 42
and 56 px text, and at 134.89, 202.33 and 269.77 px with a reader's letter spacing. It holds
for the name in lowercase in the heading typeface at weight 600; another typeface, weight or
case needs a new measurement."""
READER_LETTER_SPACING_EM = 0.12
"""The letter spacing a reader may set on all text, in ems of each element's text."""


def spacing_px(value: str | None, text: float, view: View) -> float:
    """A letter spacing in pixels for text ``text`` pixels tall: ``normal``, or a length in
    ems of that text, rem or px."""
    if value is None or value in ("normal", "0"):
        return 0.0
    read = re.fullmatch(r"(-?\d*\.?\d+)(em|rem|px)", value)
    if read is None:
        raise UnreadCss(value)
    return float(read.group(1)) * {"em": text, "rem": view.root_text, "px": 1}[read.group(2)]


def insets_of(sheet: Sheet, element: Element, view: View) -> float:
    """The margins, borders and padding an element takes from its line, in pixels."""
    total = 0.0
    for name in INSETS:
        value = value_of(sheet, element, name, view)
        if name.startswith("border"):
            total += border_px(value, view)
        elif name.startswith("padding"):
            total += padding_px(value, view)
        else:
            total += inset_px(value, view)
    return total


def brand_fits(sheet: Sheet, head: list[Element], view: View, *, wraps: bool) -> bool:
    """Whether the mark and the wordmark fit the masthead's line on ``view`` with the
    stylesheet's letter spacing and with a reader's: side by side, or each on a line of its
    own where the brand wraps. Every element above the wordmark keeps the root's text size,
    so a letter spacing it passes down in ems is of that size."""
    brand = next(one for one in head if one.tag == "a" and "brand" in one.classes)
    mark, word = (one for one in head if one.parent is brand)
    assert word.text.strip() == WORDMARK
    if any(value_of(sheet, above, "font-size", view) is not None for above in word.ancestors()):
        msg = "a text size of its own above the wordmark"
        raise UnreadCss(msg)
    for one in (word, mark, *word.ancestors()):
        if value_of(sheet, one, "zoom", view) not in (None, "1", "normal", "100%"):
            msg = f"a zoom on {one.tag} in the masthead"
            raise UnreadCss(msg)
    size = value_of(sheet, word, "font-size", view)
    text = view.root_text if size is None else length_px(size, view)
    rule = declared_for(sheet, word, "letter-spacing", view)
    passed_down = (
        rule is None
        or rule is declared_for(sheet, brand, "letter-spacing", view)
        or rule.value in ("inherit", "unset")
    )
    spacing = value_of(sheet, word, "letter-spacing", view)
    own = spacing_px(spacing, view.root_text if passed_down else text, view)
    wordmark = text * WORDMARK_EM + len(WORDMARK) * max(own, READER_LETTER_SPACING_EM * text)
    wordmark += insets_of(sheet, word, view)
    mark_px = length_px(value_of(sheet, mark, "width", view) or "", view)
    least = value_of(sheet, mark, "min-width", view)
    if least is not None and least not in ("auto", "0"):
        mark_px = max(mark_px, length_px(least, view))
    mark_px += insets_of(sheet, mark, view)
    gap = value_of(sheet, brand, "column-gap", view)
    gap_px = 0.0 if gap is None or gap in ("normal", "0") else length_px(gap, view)
    need = max(mark_px, wordmark) if wraps else mark_px + gap_px + wordmark
    need += insets_of(sheet, brand, view)
    return round(need, 6) <= round(masthead_line(sheet, head[0], view), 6)


def column(sheet: Sheet, head: Element, view: View) -> bool:
    return value_of(sheet, head, "flex-direction", view) in ("column", "column-reverse")


def brand_wraps_where_it_should(sheet: Sheet, page: str, view: View) -> bool:
    """The brand is a row of the mark and the wordmark. Where the masthead is a column, it is
    a flex line that wraps and stays within the masthead's line; where the masthead is a
    row, it keeps one line, so a wide screen keeps its masthead. Either way the app's own
    wordmark fits the line, with a reader's letter spacing too."""
    head = masthead_of(page)
    brand = [one for one in head if one.tag == "a" and "brand" in one.classes]
    assert len(brand) == 1
    assert [one.tag for one in head if one.parent is brand[0]] == ["img", "span"]
    wrapping = value_of(sheet, brand[0], "flex-wrap", view)
    along = value_of(sheet, brand[0], "flex-direction", view) in (None, "row")
    flexed = value_of(sheet, brand[0], "display", view) in ("flex", "inline-flex")
    if not brand_fits(sheet, head, view, wraps=column(sheet, head[0], view)):
        return False
    if not column(sheet, head[0], view):
        return along and flexed and wrapping in (None, "nowrap")
    return (
        along
        and flexed
        and wrapping == "wrap"
        and keeps_within_the_line(sizing_of(sheet, head[0], brand[0], view))
    )


LENGTH = re.compile(r"(\d*\.?\d+)(rem|px|%)")

ALIGNED = frozenset(
    {"normal", "stretch", "flex-start", "start", "self-start", "center", "flex-end", "end"}
    | {"self-end", "baseline"}
)


@dataclass(frozen=True)
class Sizing:
    """What decides the width of a masthead item, the brand or the page links, across a column
    masthead, as the cascade settles it: whether it wraps, its width and the least and most
    it may be, and whether the masthead stretches it across the line."""

    wraps: bool
    width: str
    least: str
    most: str
    stretched: bool
    root_text: int


def sizing_of(sheet: Sheet, head: Element, brand: Element, view: View) -> Sizing:
    align = value_of(sheet, brand, "align-self", view) or "auto"
    if align == "auto":
        align = value_of(sheet, head, "align-items", view) or "normal"
    if align not in ALIGNED:
        raise UnreadCss(align)
    return Sizing(
        value_of(sheet, brand, "flex-wrap", view) == "wrap",
        value_of(sheet, brand, "width", view) or "auto",
        value_of(sheet, brand, "min-width", view) or "auto",
        value_of(sheet, brand, "max-width", view) or "none",
        align in ("normal", "stretch"),
        view.root_text,
    )


def width_px(value: str, sizing: Sizing, line: float, content: tuple[float, float]) -> float | None:
    """A width given to the brand, in pixels, in a line ``line`` wide, for content as wide as
    ``content`` on one line and at its narrowest; None where the value sets no width."""
    one_line, narrowest = content
    if value in ("auto", "none"):
        return None
    if value == "0":
        return 0.0
    keywords = {
        "max-content": one_line,
        "min-content": narrowest,
        "fit-content": min(one_line, max(narrowest, line)),
    }
    if value in keywords:
        return keywords[value]
    read = LENGTH.fullmatch(value)
    if read is None:
        raise UnreadCss(value)
    number, unit = float(read.group(1)), read.group(2)
    if unit == "%":
        return number * line / 100
    return number * {"rem": sizing.root_text, "px": 1}[unit]


def laid_out(
    sizing: Sizing, line: float, mark: float, gap: float, word: float
) -> tuple[float, int]:
    """The brand's width and the lines the mark and the wordmark take, in a column masthead
    whose line is ``line`` wide. A browser makes it as wide as its content, or the line when
    stretched, never narrower than its widest part, within the least and most it is given."""
    one_line = mark + gap + word
    content = (one_line, max(mark, word) if sizing.wraps else one_line)
    width = width_px(sizing.width, sizing, line, content)
    if width is None:
        width = line if sizing.stretched else min(one_line, max(content[1], line))
    most = width_px(sizing.most, sizing, line, content)
    if most is not None:
        width = min(width, most)
    least = width_px(sizing.least, sizing, line, content)
    if least is not None:
        width = max(width, least)
    return width, 2 if sizing.wraps and one_line > width else 1


LINES = range(20, 1501, 20)
"""The masthead's line widths, in pixels, from far below a phone's at large text to above
a desktop's."""
MARKS = (16.0, 48.0, 160.0)
GAPS = (0.0, 10.4, 20.8)
WORDS = range(10, 1501, 20)
"""Widths of the mark, the gap and the wordmark, which the stylesheet and the font decide;
the brand holds for each mark and wordmark that fits the line."""


@functools.cache
def keeps_within_the_line(sizing: Sizing) -> bool:
    """For every line, gap, and mark and wordmark no wider than the line, the wordmarks that
    fill the line exactly and a pixel either side included, the brand is no wider than the
    line and the wordmark goes under the mark exactly when the two don't fit it together. A
    wordmark wider than its line could stay within it only by breaking the name."""
    for line in LINES:
        for mark in MARKS:
            for gap in GAPS:
                fill = line - mark - gap
                for word in (*WORDS, fill - 1, fill, fill + 1, line):
                    if word <= 0 or max(mark, word) > line:
                        continue
                    width, lines = laid_out(sizing, line, mark, gap, word)
                    if width > line or (lines == 2) != (mark + gap + word > line):
                        return False
    return True


def floor_px(value: str, form_px: float, view: View) -> float:
    """The width an entry column is never narrower than, in pixels, in a form ``form_px``
    wide: the first argument of the track's ``minmax``, a length or the ``min`` of two."""
    found = re.fullmatch(r"repeat\(auto-fit,\s*minmax\((?P<floor>.+),\s*1fr\)\)", value)
    if found is None:
        raise UnreadCss(value)
    floor = found["floor"].strip()
    least = re.fullmatch(r"min\((?P<one>[^,]+),(?P<other>[^,]+)\)", floor)
    lengths = [least["one"], least["other"]] if least else [floor]
    sizes = []
    for length in lengths:
        read = LENGTH.fullmatch(length.strip())
        if read is None:
            raise UnreadCss(length)
        number, unit = float(read.group(1)), read.group(2)
        sizes.append(
            number * {"rem": view.root_text, "px": 1}[unit]
            if unit != "%"
            else number * form_px / 100
        )
    return min(sizes)


FORM_WIDTHS = range(100, 3801, 10)


def entry_columns_fit(sheet: Sheet, page: str, view: View) -> bool:
    """An entry column is never wider than its form, and where 13rem fits, a column is
    never narrower than 13rem, which is how the form is laid out on a wide screen."""
    form = [one for one in adding_of(page) if one.attributes.get("action") == "/parent/inbox/enter"]
    value = value_of(sheet, form[0], "grid-template-columns", view)
    if value_of(sheet, form[0], "display", view) != "grid" or value is None:
        return False
    thirteen = 13 * view.root_text
    return all(
        floor_px(value, width, view) <= width
        and (width < thirteen or floor_px(value, width, view) == thirteen)
        for width in FORM_WIDTHS
    )


DATE_EM, DATE_PX = 7.027, 10.08
"""How wide a date field's text and its calendar button are together: ``DATE_EM`` ems of the
field's text plus ``DATE_PX`` pixels. That holds the widest text Edge shows while a date is
typed, 04/04/0000 once the year's first key is in, with each date part as wide as two zeros,
the widest digit in the app's body font. A year of five or six digits, which Edge also takes,
is wider than a narrow phone can show with large text."""
READER_SPACING_EM = 1.2
"""What a reader's letter spacing of 0.12em adds across the date's ten characters, in ems."""
INSETS = (
    "margin-left",
    "margin-right",
    "border-left-width",
    "border-right-width",
    "padding-left",
    "padding-right",
)


def inset_px(value: str | None, view: View) -> float:
    """A margin, border width or padding in pixels: unset, ``auto`` or ``0``; a length in px,
    rem or em (the elements here keep the root's text size) or vw; the ``min`` of such
    lengths or of their sums and differences; or a border's ``thin``, ``medium`` or ``thick``."""
    if value is None:
        return 0.0
    named = {"auto": 0.0, "0": 0.0, "thin": 1.0, "medium": 3.0, "thick": 5.0}
    if value in named:
        return named[value]
    least = re.fullmatch(r"min\((.+)\)", value)
    if least:
        return min(summed_px(one.strip(), view) for one in least.group(1).split(","))
    return length_px(value, view)


def summed_px(value: str, view: View) -> float:
    """An argument of ``min``: a length, or lengths added and taken away with a spaced sign,
    as a browser reads one. A bare number or keyword there makes the whole value unread."""
    terms = re.split(r"\s+([-+])\s+", value)
    total = length_px(terms[0], view)
    for sign, term in zip(terms[1::2], terms[2::2], strict=True):
        total += length_px(term, view) if sign == "+" else -length_px(term, view)
    return total


def length_px(value: str, view: View) -> float:
    read = re.fullmatch(r"(\d*\.?\d+)(px|rem|em|vw)", value)
    if read is None:
        raise UnreadCss(value)
    unit = {"px": 1, "rem": view.root_text, "em": view.root_text, "vw": view.width / 100}
    return float(read.group(1)) * unit[read.group(2)]


def padding_px(value: str | None, view: View) -> float:
    """A padding in pixels, which a browser never lets fall below zero."""
    return max(0.0, inset_px(value, view))


def border_px(value: str | None, view: View) -> float:
    """A border's width as a browser draws it on a screen of one pixel per CSS pixel: one
    under a pixel takes a whole pixel, and a wider one drops to whole pixels."""
    width = inset_px(value, view)
    return 0.0 if width <= 0 else max(1.0, math.floor(width + 1e-9))


def capped(sheet: Sheet, element: Element, width: float, view: View) -> float:
    """``width`` held to the element's ``max-width``, a percentage being of ``width``."""
    most = value_of(sheet, element, "max-width", view)
    if most is None or most == "none":
        return width
    share = re.fullmatch(r"(\d*\.?\d+)%", most)
    return min(width, float(share.group(1)) * width / 100 if share else inset_px(most, view))


LAYOUTS = {"form": ("grid",), "div": ("flex",), "input": (None, "block", "inline-block")}
"""How the entry form, each field's box and the date field itself are laid out, as the walk
down to a date field reads them; anything else on the way is a block."""


def laid_out_otherwise(sheet: Sheet, element: Element, view: View) -> str | None:
    """What the walk down to a date field does not read about ``element``, or None: a width or
    least width of its own, a layout other than the one it expects, columns, spacing
    between letters or words, or a zoom."""
    found = {name: value_of(sheet, element, name, view) for name in ("width", "min-width")}
    if found["width"] not in (None, "auto", "100%") or not (
        found["min-width"] in (None, "auto") or zero(found["min-width"])
    ):
        return f"width {found}"
    layout = value_of(sheet, element, "display", view)
    if layout not in LAYOUTS.get(element.tag, (None, "block")):
        return f"display {layout}"
    if element.tag == "div" and value_of(sheet, element, "flex-direction", view) != "column":
        return "a field's box that is not a column"
    spacing = [value_of(sheet, element, name, view) for name in ("letter-spacing", "word-spacing")]
    if any(not (one in (None, "normal") or zero(one)) for one in spacing):
        return f"spacing {spacing}"
    if value_of(sheet, element, "column-count", view) not in (None, "auto"):
        return "columns"
    if value_of(sheet, element, "zoom", view) not in (None, "1", "normal", "100%"):
        return "zoom"
    return None


def date_room(sheet: Sheet, page: str, view: View) -> list[float]:
    """For each date field of Add assignments, how much wider than its widest text and its
    calendar button, with a reader's letter spacing, the field's text box is on ``view``, in
    pixels; below zero, the field clips a date being edited. Each element on the way down
    from the page takes its margins, borders and padding from the width, and the entry
    form's column is never narrower than its floor."""
    fields = [one for one in adding_of(page) if one.attributes.get("type") == "date"]
    assert [one.attributes["id"] for one in fields] == ["entry-assigned_on", "entry-due_date"]
    room = []
    for one in fields:
        chain = [one, *one.ancestors()]
        if any(value_of(sheet, above, "font-size", view) is not None for above in chain):
            msg = "a text size of its own on the way to a date field"
            raise UnreadCss(msg)
        if any(value_of(sheet, above, "box-sizing", view) != "border-box" for above in chain):
            msg = "a width that is not the border box on the way to a date field"
            raise UnreadCss(msg)
        for above in chain:
            unread = laid_out_otherwise(sheet, above, view)
            if unread:
                msg = f"{above.tag} on the way to a date field: {unread}"
                raise UnreadCss(msg)
        width = float(view.width)
        for above in reversed(chain):
            width -= sum(inset_px(value_of(sheet, above, name, view), view) for name in INSETS[:2])
            width = capped(sheet, above, width, view)
            width -= sum(
                (border_px if name.startswith("border") else padding_px)(
                    value_of(sheet, above, name, view), view
                )
                for name in INSETS[2:]
            )
            if above.tag == "form":
                columns = value_of(sheet, above, "grid-template-columns", view)
                width = floor_px(columns or "", width, view)
        need = (DATE_EM + READER_SPACING_EM) * view.root_text + DATE_PX
        room.append(round(width - need, 6))
    return room


ADDING_CONTROLS = [
    ("summary", "Add assignments"),
    ("label", "School text"),
    ("button", "Preview assignments"),
    ("label", "Course"),
    ("label", "Assignment title"),
    ("label", "Assigned date (optional)"),
    ("label", "Due date (optional)"),
    ("label", "Type (optional)"),
    ("label", "Note (optional)"),
    ("button", "Preview assignment"),
]


def words_break_where_they_must(sheet: Sheet, page: str, view: View) -> list[str]:
    """The fold's summary and the labels and buttons of Add assignments whose words do not
    break when one is wider than its line. `anywhere` also lets the control be narrower
    than its longest word, which `break-word` does not, so a button inside the card never
    pushes past it."""
    found = [one for one in entry_surface_of(page) if one.tag in ("summary", "label", "button")]
    assert [(one.tag, " ".join(one.text.split())) for one in found] == ADDING_CONTROLS
    return [
        " ".join(one.text.split())
        for one in found
        if value_of(sheet, one, "overflow-wrap", view) != "anywhere"
    ]


CUT = {
    "white-space": ("nowrap", "pre"),
    "text-wrap": ("nowrap",),
    "text-wrap-mode": ("nowrap",),
    "overflow": ("hidden", "clip", "auto", "scroll"),
    "overflow-x": ("hidden", "clip", "auto", "scroll"),
    "text-overflow": ("ellipsis", "clip"),
}


def cut_or_shrunk(sheet: Sheet, held: list[Element]) -> list[str]:
    """Each element that some screen keeps on one line, cuts off, or gives a text size or
    zoom of its own that another screen does not, and each element above them that hides or
    scrolls what runs past it. A visually hidden word is clipped by design and is passed
    over."""
    found = []
    for one in held:
        if "visually-hidden" in one.classes:
            continue
        for size in ("font-size", "zoom"):
            sizes = {getattr(declared_for(sheet, one, size, view), "order", None) for view in VIEWS}
            if len(sizes) > 1:
                found.append(f"{one.tag}.{'.'.join(sorted(one.classes))} {size}")
        for name, refused in CUT.items():
            for view in VIEWS:
                if value_of(sheet, one, name, view) in refused:
                    found.append(f"{one.tag}.{'.'.join(sorted(one.classes))} {name} {view}")
    above = {id(one): one for held_one in held for one in held_one.ancestors()}
    for one in above.values():
        if "visually-hidden" in one.classes:
            continue
        for name in ("overflow", "overflow-x"):
            for view in VIEWS:
                if value_of(sheet, one, name, view) in CUT[name]:
                    found.append(f"{one.tag}.{'.'.join(sorted(one.classes))} {name} {view}")
    return found


# ------------------------------------------------------------- the pages


RENDERED: dict[str, str] = {}


@pytest.fixture
def rendered(tmp_path: pathlib.Path) -> dict[str, str]:
    """The sign-in page; her week as she reads it; her week and the family page as a
    signed-in parent reads them; and both with the sign-in off. Rendered by the first test
    that asks and kept for the rest, since nothing here changes them."""
    if RENDERED:
        return RENDERED
    for reader in ("her", "parent", "open"):
        (tmp_path / reader).mkdir()
        with household_client(reader, tmp_path / reader) as client:
            if reader == "her":
                RENDERED["sign-in page"] = client.get("/sign-in", headers=PAGE_HEADERS).text
            sign_in_as(client, reader)
            RENDERED[f"{reader}, her week"] = client.get(HER_PAGE, headers=PAGE_HEADERS).text
            if reader != "her":
                RENDERED[f"{reader}, family"] = client.get("/parent", headers=PAGE_HEADERS).text
    return RENDERED


OPENED: dict[str, str] = {}


@pytest.fixture
def opened(tmp_path: pathlib.Path) -> dict[str, str]:
    """The family page as it comes back when the pasted text or the one assignment is
    refused, with Add assignments open, for a signed-in parent and with the sign-in off."""
    if OPENED:
        return OPENED
    for reader in ("parent", "open"):
        folder = tmp_path / "opened" / reader
        folder.mkdir(parents=True)
        with household_client(reader, folder) as client:
            sign_in_as(client, reader)
            for form, data in (
                ("read", {"text": ""}),
                ("enter", {"course": "Geometry", "title": ""}),
            ):
                answer = client.post(f"/parent/inbox/{form}", data=data, headers=PAGE_HEADERS)
                assert answer.status_code == 422
                assert '<details class="steps panel-fold" id="add-assignments" open>' in answer.text
                OPENED[f"{reader}, {form} refused, family"] = answer.text
    return OPENED


def with_nav(rendered: dict[str, str]) -> dict[str, str]:
    return {name: page for name, page in rendered.items() if name != "sign-in page"}


def family(rendered: dict[str, str]) -> dict[str, str]:
    return {name: page for name, page in rendered.items() if name.endswith("family")}


# ------------------------------------------------------------- the tests


def test_the_resolver_reads_the_cascade_the_way_a_browser_would() -> None:
    """An id beats a class, a class beats a type, a later rule beats an earlier one of the
    same weight, a state never holds, and an inherited property comes down from the nearest
    element that sets it while one that is not inherited does not. A media query measures
    `rem` in the browser's text size, never the page's. What it cannot evaluate, it
    refuses."""
    sheet = read_sheet(
        """
        p { overflow-wrap: normal; flex-wrap: nowrap; }
        .x { overflow-wrap: break-word; }
        #y { overflow-wrap: anywhere; }
        .x { flex-wrap: wrap; }
        p:hover { flex-wrap: wrap-reverse; }
        section { overflow-wrap: anywhere; flex-wrap: wrap; }
        @media (max-width: 30rem) { .x { white-space: nowrap; } }
        """
    )
    page = elements_of('<section><p class="x" id="y">a</p><p class="x">b</p><b>c</b></section>')
    _, one, other, bold = page
    phone = View(320)

    assert value_of(sheet, one, "overflow-wrap", phone) == "anywhere"
    assert value_of(sheet, other, "overflow-wrap", phone) == "break-word"
    assert value_of(sheet, other, "flex-wrap", phone) == "wrap"
    assert value_of(sheet, bold, "overflow-wrap", phone) == "anywhere"
    assert value_of(sheet, bold, "flex-wrap", phone) is None
    assert value_of(sheet, bold, "white-space", phone) is None
    assert value_of(sheet, other, "white-space", View(480)) == "nowrap"
    assert value_of(sheet, other, "white-space", View(481)) is None
    assert value_of(sheet, other, "white-space", View(960, 32, 32)) == "nowrap"
    assert value_of(sheet, other, "white-space", View(960, 16, 32)) is None
    for unread in (
        "@media (hover: hover) { p { flex-wrap: wrap; } }",
        "@media (min-width: 40vw) { p { flex-wrap: wrap; } }",
        "@supports (display: grid) { p { flex-wrap: wrap; } }",
        "p:focus-within, p:last-child { flex-wrap: wrap; }",
        "p + p { flex-wrap: wrap; }",
        "p { flex-flow: wrap dense; }",
        "p { & { flex-wrap: wrap; } }",
    ):
        with pytest.raises(UnreadCss):
            value_of(read_sheet(unread), one, "flex-wrap", phone)
    sizes = read_sheet(
        """
        p { width: 10px; inline-size: max-content; min-inline-size: 0; place-self: center end; }
        .x { max-inline-size: 100%; max-width: none; }
        section { place-items: stretch; width: 5px; }
        """
    )
    assert value_of(sizes, other, "width", phone) == "max-content"
    assert value_of(sizes, other, "min-width", phone) == "0"
    assert value_of(sizes, other, "max-width", phone) == "none"
    assert value_of(sizes, other, "align-self", phone) == "center"
    assert value_of(sizes, page[0], "align-items", phone) == "stretch"
    assert value_of(sizes, bold, "width", phone) is None
    spelled = read_sheet(
        """
        p { WIDTH: 10PX ! Important; }
        section .x { width: 20px; }
        .x { Min-Width: 1px!important; min-width: 2px; Align-Self: STRETCH; }
        p.x:link { max-width: 3px; }
        section .x { max-width: 4px; }
        """
    )
    assert value_of(spelled, other, "width", phone) == "10px"
    assert value_of(spelled, other, "min-width", phone) == "1px"
    assert value_of(spelled, other, "align-self", phone) == "stretch"
    assert value_of(spelled, other, "max-width", phone) == "4px"
    assert compound(":root").specificity == (0, 1, 0)
    assert compound("html:root").specificity == (0, 1, 1)
    assert not compound(":root").matches(other)
    assert (
        floor_px("repeat(auto-fit, minmax(min(13rem, 100%), 1fr))", 161, View(320, 16, 32)) == 161
    )
    assert floor_px("repeat(auto-fit, minmax(13rem, 1fr))", 161, View(320, 16, 32)) == 416


@pytest.mark.parametrize(
    ("query", "holding", "failing"),
    [
        pytest.param("@MEDIA SCREEN AND (MAX-WIDTH: 30REM)", View(480), View(481), id="capitals"),
        pytest.param(
            "@media screen and (max-width: 30REM)",
            View(960, 32, 32),
            View(961, 32, 32),
            id="unit-in-capitals",
        ),
        pytest.param(
            "@Media Only Screen And (Min-Width: 72Rem)", View(1152), View(1151), id="mixed-case"
        ),
        pytest.param("@media ALL and (max-width: 480PX)", View(480), View(481), id="pixels"),
        pytest.param(
            "@media (Max-Width: 30Em)", View(480, 16, 32), View(481, 16, 32), id="em-units"
        ),
        pytest.param(
            "@media (PREFERS-REDUCED-MOTION: REDUCE)",
            View(820, reduced_motion=True),
            View(820),
            id="less-motion",
        ),
        pytest.param(
            "@media (prefers-reduced-motion: No-Preference)",
            View(820),
            View(820, reduced_motion=True),
            id="motion",
        ),
        pytest.param("@MEDIA PRINT", None, View(820), id="print"),
    ],
)
def test_the_resolver_reads_a_media_query_in_any_case(
    query: str, holding: View | None, failing: View
) -> None:
    """A media query's types, features, values and units are read in any case."""
    sheet = read_sheet(f"{query} {{ p {{ flex-wrap: wrap; }} }}")
    (paragraph,) = elements_of("<p>a</p>")
    if holding is not None:
        assert value_of(sheet, paragraph, "flex-wrap", holding) == "wrap"
        assert one_query_holds(query.partition(" ")[2], holding)
    assert value_of(sheet, paragraph, "flex-wrap", failing) is None
    assert not one_query_holds(query.partition(" ")[2], failing)


def test_a_media_length_is_read_in_any_case() -> None:
    assert media_length("30REM", View(320, 32)) == 960
    assert media_length("30Em", View(320, 24)) == 720
    assert media_length("480PX", View(320, 32)) == 480
    with pytest.raises(UnreadCss):
        media_length("40VW", View(320))


@pytest.mark.parametrize(
    "query",
    [
        "@MEDIA NOT SCREEN",
        "@media (HOVER: HOVER)",
        "@media (MIN-WIDTH: 40VW)",
        "@media (PREFERS-REDUCED-MOTION: REDUCED)",
        "@media (PREFERS-REDUCED-MOTION)",
        "@media (MAX-W\N{LATIN CAPITAL LETTER I WITH DOT ABOVE}DTH: 30rem)",
        "@media \N{LATIN SMALL LETTER LONG S}creen and (max-width: 30rem)",
        "@media (max-width: 30\uff32\uff25\uff2d)",
    ],
)
def test_the_resolver_refuses_a_media_query_it_cannot_read_in_any_case(query: str) -> None:
    (paragraph,) = elements_of("<p>a</p>")
    with pytest.raises(UnreadCss):
        value_of(
            read_sheet(f"{query} {{ p {{ flex-wrap: wrap; }} }}"),
            paragraph,
            "flex-wrap",
            View(320),
        )
    with pytest.raises(UnreadCss):
        one_query_holds(query.partition(" ")[2], View(320))


@pytest.mark.parametrize(
    ("query", "holding", "failing"),
    [
        pytest.param("only screen and (max-width: 30rem)", View(480), View(481), id="only"),
        pytest.param(
            "screen and (min-width: 1px) and (max-width: 30rem)",
            View(480),
            View(481),
            id="two-features",
        ),
        pytest.param(
            "(min-width: 1px) and (max-width: 30rem)", View(480), View(481), id="features-alone"
        ),
        pytest.param("(max-width:30rem)", View(480), View(481), id="no-spaces"),
        pytest.param("print, screen and (max-width: 30rem)", View(480), View(481), id="list"),
        pytest.param(
            "screen\tand\n(max-width:\r30rem)\f", View(480), View(481), id="every-css-space"
        ),
        pytest.param("ONLY ALL", View(3840), None, id="only-all"),
        pytest.param("", View(320), None, id="empty-list"),
        pytest.param(" \t", View(320), None, id="spaces-only-list"),
        pytest.param(
            "(max-width:30rem)and (min-width:1px)", View(480), View(481), id="and-after-a-bracket"
        ),
    ],
)
def test_the_resolver_reads_each_form_of_media_query_a_browser_reads(
    query: str, holding: View, failing: View | None
) -> None:
    (paragraph,) = elements_of("<p>a</p>")
    sheet = read_sheet(f"@media {query} {{ p {{ flex-wrap: wrap; }} }}")
    assert value_of(sheet, paragraph, "flex-wrap", holding) == "wrap"
    assert condition_holds(query, holding)
    if failing is not None:
        assert value_of(sheet, paragraph, "flex-wrap", failing) is None
        assert not condition_holds(query, failing)


@pytest.mark.parametrize(
    "query",
    [
        "print,",
        ",print",
        "print,,print",
        "and",
        "only",
        "screen and",
        "and screen",
        "only and",
        "AND (MAX-WIDTH: 30REM)",
        "only (max-width: 30rem)",
        "screen screen print",
        "screen (max-width: 30rem)",
        "(max-width: 30rem) (min-width: 1px)",
        "(max-width: 30rem) and",
        "SCREEN AND AND (MAX-WIDTH: 30REM)",
        "screen and(max-width: 30rem)",
        "max-width: 30rem)",
    ],
)
def test_the_resolver_refuses_a_media_query_a_browser_drops(query: str) -> None:
    (paragraph,) = elements_of("<p>a</p>")
    sheet = read_sheet(f"@media {query} {{ p {{ flex-wrap: wrap; }} }}")
    for view in (View(320), View(480)):
        with pytest.raises(UnreadCss):
            value_of(sheet, paragraph, "flex-wrap", view)
        with pytest.raises(UnreadCss):
            condition_holds(query, view)


@pytest.mark.parametrize("query", ["(max-width: 30rem", "screen and (max-width: 30rem"])
def test_the_resolver_refuses_a_media_query_left_open_before_its_block(query: str) -> None:
    """Edge reads the block after `(max-width: 30rem` as part of the open bracket, so no rule
    is drawn; the sheet is refused, and so is the query alone."""
    with pytest.raises(UnreadCss):
        read_sheet(f"@media {query} {{ p {{ flex-wrap: wrap; }} }}")
    for view in (View(320), View(480)):
        with pytest.raises(UnreadCss):
            condition_holds(query, view)


@pytest.mark.parametrize(
    "written", ["@mediascreen", "@MEDIAALL", "@media-x screen", "@media_ screen", "@media\\ all"]
)
def test_the_resolver_refuses_an_at_rule_that_only_starts_like_a_media_query(
    written: str,
) -> None:
    (paragraph,) = elements_of("<p>a</p>")
    with pytest.raises(UnreadCss):
        value_of(
            read_sheet(f"{written} {{ p {{ flex-wrap: wrap; }} }}"),
            paragraph,
            "flex-wrap",
            View(320),
        )


def test_the_resolver_reads_a_media_query_right_after_its_keyword() -> None:
    (paragraph,) = elements_of("<p>a</p>")
    sheet = read_sheet("@MEDIA(MAX-WIDTH:30REM){ p { flex-wrap: wrap; } }")
    assert value_of(sheet, paragraph, "flex-wrap", View(480)) == "wrap"
    assert value_of(sheet, paragraph, "flex-wrap", View(481)) is None


KELVIN = "\N{KELVIN SIGN}"
STRANGERS = {
    "no-break space": "\u00a0",
    "em space": "\u2003",
    "ideographic space": "\u3000",
    "next line": "\u0085",
    "vertical tab": "\v",
    "unit separator": "\x1f",
    "delete": "\x7f",
    "first past ascii": "\x80",
    "kelvin sign": KELVIN,
}
"""Characters Python reads as a space, or as a letter of another case, where a browser reads
neither: each is outside printable ASCII and is no CSS space."""


@pytest.mark.parametrize("stranger", list(STRANGERS.values()), ids=list(STRANGERS))
def test_the_resolver_refuses_a_character_a_browser_reads_otherwise(stranger: str) -> None:
    view = View(320)
    for css in (
        f"@media screen{stranger}and (max-width: 30rem) {{ p {{ flex-wrap: wrap; }} }}",
        f"@media (max-width: 30rem{stranger}) {{ p {{ flex-wrap: wrap; }} }}",
        f"p{stranger}a {{ flex-wrap: wrap; }}",
        f"p {{ flex-wrap:{stranger}wrap; }}",
        f"p {{ flex-wrap{stranger}: wrap; }}",
    ):
        with pytest.raises(UnreadCss):
            read_sheet(css)
    for query in (
        f"screen{stranger}and (max-width: 30rem)",
        f"{stranger}screen",
        f"(max-width: 30rem{stranger})",
    ):
        with pytest.raises(UnreadCss):
            condition_holds(query, view)
        with pytest.raises(UnreadCss):
            one_query_holds(query, view)
    with pytest.raises(UnreadCss):
        media_length(f"30rem{stranger}", view)
    with pytest.raises(UnreadCss):
        selector(f"p{stranger}a")


@pytest.mark.parametrize(
    "number",
    ["\u0663\u0660", "\uff13\uff10", "3\u0660", "\u06630"],
    ids=["arabic-indic", "fullwidth", "ascii-first", "ascii-last"],
)
def test_the_resolver_reads_only_ascii_digits(number: str) -> None:
    with pytest.raises(UnreadCss):
        media_length(f"{number}rem", View(320))
    with pytest.raises(UnreadCss):
        condition_holds(f"(max-width: {number}rem)", View(320))
    with pytest.raises(UnreadCss):
        read_sheet(f"p {{ width: {number}px; }}")
    with pytest.raises(UnreadCss):
        compound(f"p:nth-child({number})")


def test_the_resolver_reads_no_other_letter_as_a_keywords_own() -> None:
    dotted = "\N{LATIN CAPITAL LETTER I WITH DOT ABOVE}"
    for css in (
        f"p {{ overflow-wrap: brea{KELVIN}-word; }}",
        f"p {{ overflow-wrap: BREA{KELVIN}-WORD; }}",
        f"p {{ word-brea{KELVIN}: break-all; }}",
        f"p {{ overflow-wrap: anywhere !{dotted}mportant; }}",
        f"p:lin{KELVIN} {{ overflow-wrap: anywhere; }}",
        f"@media screen {{ p {{ display: bloc{KELVIN}; }} }}",
    ):
        with pytest.raises(UnreadCss):
            read_sheet(css)
    with pytest.raises(UnreadCss):
        compound(f"a:lin{KELVIN}")


def test_the_resolver_refuses_a_stand_in_written_outside_a_comment() -> None:
    (paragraph,) = elements_of("<p>a</p>")
    for stand_in in sorted(STAND_INS):
        for css in (
            f"p{stand_in}first-child {{ flex-wrap: wrap; }}",
            f"p {{ flex-wrap: wrap{stand_in} }}",
            f'p {{ content: "{stand_in}"; flex-wrap: wrap; }}',
            f"p {{ --x: a\\{stand_in}b; flex-wrap: wrap; }}",
        ):
            with pytest.raises(UnreadCss):
                read_sheet(css)
        with pytest.raises(UnreadCss):
            selector(f"p{stand_in}first-child")
        sheet = read_sheet(f"/* {stand_in} */ p {{ flex-wrap: wrap; }}")
        assert value_of(sheet, paragraph, "flex-wrap", View(320)) == "wrap"


def test_the_resolver_reads_a_shielded_character_in_a_string_as_itself() -> None:
    (paragraph,) = elements_of('<p title="a,b:c;d{e}">a</p>')
    sheet = read_sheet('p[title="a,b:c;d{e}"] { content: "x;y"; flex-wrap: wrap; }')
    assert value_of(sheet, paragraph, "flex-wrap", View(320)) == "wrap"


def test_the_resolver_reads_printable_ascii_and_passes_over_comments() -> None:
    (paragraph,) = elements_of("<p>a</p>")
    sheet = read_sheet(
        f'/* {KELVIN}\u00a0\u0663 */ p {{ content: "~ !"; flex-wrap: /* \u3000 */ wrap; }}'
    )
    assert value_of(sheet, paragraph, "flex-wrap", View(320)) == "wrap"


@pytest.mark.parametrize(
    ("name", "kept", "dropped"),
    [
        *(
            ("flex-wrap", "wrap", tail)
            for tail in (
                "nowrap !importantx",
                "nowrap !important!",
                "nowrap !important x",
                "nowrap !!important",
                "nowrap !imp",
                "nowrap important",
                "nowrap !important !important",
                "nowrap ! important x",
                "nowrap !",
                "!important",
                "!important nowrap",
                "now!rap",
                "nowrap nowrap",
                "banana",
            )
        ),
        ("flex-direction", "column", "row column"),
        ("box-sizing", "border-box", "padding-box"),
        ("overflow-wrap", "anywhere", "break-all"),
        ("width", "1px", "min(2px, !)"),
        ("width", "1px", "var(--a) !x"),
        ("width", "1px", "f(var(--a)) !x"),
        ("flex-wrap", "wrap", "calc(1)"),
        ("flex-wrap", "wrap", "myvar(--mode)"),
        ("flex-wrap", "wrap", "-var(--mode)"),
    ],
)
def test_the_resolver_drops_a_declaration_a_browser_drops(
    name: str, kept: str, dropped: str
) -> None:
    """Edge drops each declaration below and keeps the one before it: a value with a `!`
    left once its `!important` is taken off, and a value a property of one keyword does not
    take."""
    (paragraph,) = elements_of("<p>a</p>")
    sheet = read_sheet(f"p {{ {name}: {kept}; }} p {{ {name}: {dropped}; }}")
    assert value_of(sheet, paragraph, name, View(320)) == kept


@pytest.mark.parametrize(
    ("name", "value"),
    [
        ("flex-wrap", "var(--mode)"),
        ("flex-wrap", "nowr\\61p"),
        ("overflow-wrap", "VAR(--x)"),
        ("word-wrap", "var(--x)"),
        ("flex-direction", "env(x)"),
        ("box-sizing", "attr(x)"),
        ("flex-wrap", "--f(1)"),
        ("box-sizing", "--f(1)"),
        ("flex-wrap", "if(else: nowrap)"),
    ],
)
def test_a_property_of_one_keyword_refuses_a_value_that_may_stand_for_one(
    name: str, value: str
) -> None:
    """Edge reads `flex-wrap: var(--mode)` as the property's value, `nowrap` here, and
    `nowr\\61p` as `nowrap`."""
    with pytest.raises(UnreadCss):
        read_sheet(f":root {{ --mode: nowrap; }} p {{ {name}: wrap; }} p {{ {name}: {value}; }}")


@pytest.mark.parametrize(
    "declaration",
    [
        "color: var(--ink, f(!))",
        "color: VAR(--ink, f(!))",
        "width: calc(var(--a) + f(!))",
        "flex-wrap: env(x, f(!))",
        "color: \\76 ar(--ink, f(!))",
        "color: v\\61r(--ink, f(!))",
        "color: \\45 nv(x, f(!))",
    ],
)
def test_the_resolver_refuses_a_bang_inside_a_substitution(declaration: str) -> None:
    """Edge keeps `color: var(--ink, f(!))`: the `!` sits in a fallback it may never use."""
    with pytest.raises(UnreadCss):
        style_rules(f"p {{ {declaration}; }}")


def test_a_property_of_one_keyword_takes_its_other_name_and_every_propertys_keywords() -> None:
    """Edge drops `word-wrap: break-all` and reads `flex-wrap: initial`."""
    (paragraph,) = elements_of("<p>a</p>")
    sheet = read_sheet("p { overflow-wrap: anywhere; } p { word-wrap: break-all; }")
    assert value_of(sheet, paragraph, "overflow-wrap", View(320)) == "anywhere"
    sheet = read_sheet("p { flex-wrap: wrap; } p { flex-wrap: initial; }")
    assert value_of(sheet, paragraph, "flex-wrap", View(320)) is None


@pytest.mark.parametrize(
    ("css", "name", "value"),
    [
        *(
            pytest.param(
                f"p {{ display: flex; flex-wrap: {before}; }} p {{ flex-flow: {flow}; }}",
                "flex-wrap",
                value,
                id=flow,
            )
            for before, flow, value in (
                ("nowrap", "wrap wrap", "nowrap"),
                ("wrap", "nowrap nowrap", "wrap"),
                ("nowrap", "wrap nowrap", "nowrap"),
                ("nowrap", "wrap row wrap", "nowrap"),
                ("nowrap", "row wrap wrap", "nowrap"),
                ("nowrap", "row row wrap", "nowrap"),
                ("nowrap", "row column wrap", "nowrap"),
                ("nowrap", "row wrap column", "nowrap"),
                ("nowrap", "wrap-reverse wrap", "nowrap"),
                ("nowrap", "wrap", "wrap"),
                ("nowrap", "row wrap", "wrap"),
                ("nowrap", "wrap row", "wrap"),
                ("nowrap", "column-reverse wrap-reverse", "wrap-reverse"),
                ("nowrap", "wrap/**/row", "wrap"),
                ("nowrap", "WRAP Row", "wrap"),
                ("wrap", "column", "nowrap"),
            )
        ),
        *(
            pytest.param(
                f"p {{ display: flex; flex-direction: {before}; }} p {{ flex-flow: {flow}; }}",
                "flex-direction",
                value,
                id=f"direction-{flow}",
            )
            for before, flow, value in (
                ("column", "row column", "column"),
                ("row", "column column", "row"),
                ("row", "wrap column", "column"),
            )
        ),
        pytest.param(
            "p { flex-flow: wrap !important; } p { flex-flow: nowrap nowrap !important; }",
            "flex-wrap",
            "wrap",
            id="important",
        ),
    ],
)
def test_the_resolver_reads_flex_flow_as_a_browser_reads_it(
    css: str, name: str, value: str
) -> None:
    """Edge takes at most one direction and one wrapping keyword in `flex-flow`, in either
    order, and drops a value with two of either, keeping the declaration before it."""
    (paragraph,) = elements_of("<p>a</p>")
    assert value_of(read_sheet(css), paragraph, name, View(320)) == value


@pytest.mark.parametrize("flow", ["initial", "wrap initial", "wrap, row"])
def test_the_resolver_refuses_a_flex_flow_it_does_not_read(flow: str) -> None:
    """Edge reads `flex-flow: initial` and drops the other two; the reader refuses each."""
    with pytest.raises(UnreadCss):
        read_sheet(f"p {{ flex-wrap: wrap; }} p {{ flex-flow: {flow}; }}")


@pytest.mark.parametrize(
    ("css", "value"),
    [
        pytest.param("} p { flex-wrap: wrap; }", None, id="at-the-start"),
        pytest.param("} p { flex-wrap: nowrap; } p { flex-wrap: wrap; }", "wrap", id="then-a-rule"),
        pytest.param(
            "p { flex-wrap: nowrap; } } p { flex-wrap: nowrap; } p { flex-wrap: wrap; }",
            "wrap",
            id="between-rules",
        ),
        pytest.param(
            "p { flex-wrap: wrap; } } p { flex-wrap: nowrap; }", "wrap", id="earlier-kept"
        ),
        pytest.param(
            "} } p { flex-wrap: nowrap; } p { flex-wrap: wrap; }", "wrap", id="two-in-a-row"
        ),
        pytest.param(
            "} p { flex-wrap: nowrap; } } p { flex-wrap: nowrap; } p { flex-wrap: wrap; }",
            "wrap",
            id="two-apart",
        ),
        pytest.param(
            "x } p { flex-wrap: nowrap; } p { flex-wrap: wrap; }", "wrap", id="word-before"
        ),
        pytest.param(
            "} ; p { flex-wrap: nowrap; } p { flex-wrap: wrap; }", "wrap", id="semicolon-after"
        ),
        pytest.param(
            "} @media (min-width: 1px) { p { flex-wrap: nowrap; } } p { flex-wrap: wrap; }",
            "wrap",
            id="before-a-media-rule",
        ),
        pytest.param(
            "} @font-face { font-family: x; } p { flex-wrap: wrap; }",
            "wrap",
            id="before-an-at-rule",
        ),
        pytest.param(
            "@media (min-width: 1px) { p { flex-wrap: wrap; } } } p { flex-wrap: nowrap; }"
            " p { flex-wrap: wrap-reverse; }",
            "wrap-reverse",
            id="after-a-media-rule",
        ),
        pytest.param(
            "@media (min-width: 1px) { p { flex-wrap: nowrap; } } p { flex-wrap: wrap; } }",
            "wrap",
            id="at-the-end-after-a-media-rule",
        ),
        pytest.param("p { flex-wrap: wrap; } }", "wrap", id="at-the-end"),
        pytest.param(
            "} p { flex-wrap: nowrap; } p { flex-wrap: wrap;", "wrap", id="then-open-at-the-end"
        ),
        pytest.param(
            "p { flex-wrap: wrap; } } p { flex-wrap: nowrap;", "wrap", id="before-open-at-the-end"
        ),
        pytest.param("}", None, id="alone"),
        pytest.param(
            'p[title="}"] { flex-wrap: nowrap; } p { flex-wrap: wrap; }', "wrap", id="in-a-string"
        ),
        pytest.param("/* } */ p { flex-wrap: wrap; }", "wrap", id="in-a-comment"),
    ],
)
def test_the_resolver_drops_only_the_rule_a_stray_closing_brace_starts(
    css: str, value: str | None
) -> None:
    """Edge reads a `}` with no block open as part of the next rule's selector, drops that
    rule, and reads the rest."""
    (paragraph,) = elements_of("<p>a</p>")
    assert value_of(read_sheet(css), paragraph, "flex-wrap", View(320)) == value


def test_the_resolver_refuses_the_mark_of_a_broken_string_written_in_a_sheet() -> None:
    with pytest.raises(UnreadCss):
        read_sheet("p { flex-wrap: wrap\ue006; }")


@pytest.mark.parametrize(
    "tail",
    [
        "nowrap !important",
        "nowrap ! important",
        "nowrap !IMPORTANT",
        "nowrap !/**/important",
        "nowrap !\nimportant",
        "nowrap!important",
        "nowrap !important /* x */",
    ],
)
def test_the_resolver_reads_each_form_of_importance_a_browser_reads(tail: str) -> None:
    (paragraph,) = elements_of("<p>a</p>")
    sheet = read_sheet(f"p {{ flex-wrap: {tail}; }} p {{ flex-wrap: wrap; }}")
    assert value_of(sheet, paragraph, "flex-wrap", View(320)) == "nowrap"


def test_the_resolver_refuses_a_bang_beside_an_escape() -> None:
    """Edge reads `!imp\\ortant` as `!important`, an escaped letter being the letter."""
    with pytest.raises(UnreadCss):
        read_sheet("p { flex-wrap: nowrap !imp\\ortant; } p { flex-wrap: wrap; }")


@pytest.mark.parametrize(
    "declaration",
    [
        pytest.param("flex-wrap: nowrap !\\69mportant", id="hex-letter-after-the-bang"),
        pytest.param("flex-wrap: nowrap ! imp\\ortant", id="space-then-escaped-letter"),
        pytest.param("flex-wrap: nowrap !/**/imp\\ortant", id="comment-then-escaped-letter"),
        pytest.param("flex-wrap: nowrap \\!important", id="escaped-bang-important"),
        pytest.param("--x: a \\!x", id="escaped-bang"),
        pytest.param("--x: a\\!important", id="escaped-bang-touching"),
        pytest.param("--x: a\\\\!important", id="escaped-backslash-then-important"),
        pytest.param("--x: a\\ !important", id="escaped-space-then-important"),
        pytest.param("--x: f(!) b !\\69mportant", id="custom-hex-letter-after-the-bang"),
    ],
)
def test_the_resolver_refuses_a_bang_an_escape_touches(declaration: str) -> None:
    """Edge reads each `!` here as part of an escape, or its `!important` with an escape
    beside it, as the reader cannot."""
    with pytest.raises(UnreadCss):
        style_rules(f"p {{ {declaration}; flex-wrap: wrap; }}")


@pytest.mark.parametrize(
    ("declaration", "read"),
    [
        pytest.param("--x: f(!) a\\41", ("--x", "f(!) a\\41", False), id="bang-then-escape"),
        pytest.param("--x: a\\41 f(!)", ("--x", "a\\41 f(!)", False), id="escape-then-bang"),
        pytest.param("--x: foo\\ bar !important", ("--x", "foo\\ bar", True), id="important"),
        pytest.param("--x: a\\41!important", ("--x", "a\\41", True), id="hex-then-important"),
        pytest.param("--x: f(a\\) !x)", ("--x", "f(a\\) !x)", False), id="escaped-close"),
        pytest.param("--x: [a\\] !x]", ("--x", "[a\\] !x]", False), id="escaped-square"),
        pytest.param("--x: f(a\\29 !x)", ("--x", "f(a\\29 !x)", False), id="hex-close"),
        pytest.param("--x: a\\( !x", None, id="escaped-open"),
        pytest.param("--x: a\\28 !x", None, id="hex-open"),
        pytest.param("--x: a\\0000028 !x", None, id="seven-digits-then-bang"),
        pytest.param("--x: a\\41!x", None, id="hex-then-bang"),
        pytest.param("--x: a\\\\!x", None, id="escaped-backslash-then-bang"),
        pytest.param('content: "\\41" !x', None, id="string-escape-then-bang"),
        pytest.param("flex-wrap: nowrap\\ !x", None, id="escaped-space-then-bang"),
    ],
)
def test_the_resolver_reads_a_bang_apart_from_an_escape_as_a_browser_reads_it(
    declaration: str, read: tuple[str, str, bool] | None
) -> None:
    """Edge reads an escape as text, an escaped bracket too, and keeps or drops each
    declaration here by its `!` alone."""
    (rule,) = style_rules(f"p {{ {declaration}; flex-wrap: wrap; }}")
    assert rule.declarations == (*((read,) if read else ()), ("flex-wrap", "wrap", False))


@pytest.mark.parametrize(
    ("declaration", "read"),
    [
        pytest.param("--x: f(] !x)", None, id="paren-then-square"),
        pytest.param("--x: [) !x]", None, id="square-then-paren"),
        pytest.param("--x: f(] !x) !important", None, id="paren-then-square-important"),
        pytest.param("--x: f(a] b) !x", None, id="bang-after-the-bracket"),
        pytest.param("--x: f([a] !x)", ("--x", "f([a] !x)", False), id="matched"),
    ],
)
def test_the_resolver_drops_a_bang_after_a_closing_bracket_of_the_other_kind(
    declaration: str, read: tuple[str, str, bool] | None
) -> None:
    """Edge drops a custom property holding a closing bracket of the other kind, `!` and all."""
    (rule,) = style_rules(f"p {{ {declaration}; flex-wrap: wrap; }}")
    assert rule.declarations == (*((read,) if read else ()), ("flex-wrap", "wrap", False))


@pytest.mark.parametrize(
    ("declaration", "read"),
    [
        pytest.param("--x: f([) !x])", None, id="nested-with-bang"),
        pytest.param("--x: [(] !x)]", None, id="square-nested-with-bang"),
        pytest.param("--x: f([)])", None, id="nested"),
        pytest.param("--x: [f(]])]", None, id="nested-square"),
        pytest.param("--x: f(g([h(])]))", None, id="deep"),
        pytest.param("--x: a) b", None, id="stray-paren"),
        pytest.param("--x: a] b", None, id="stray-square"),
        pytest.param("--x: (a] b)", None, id="paren-then-square"),
        pytest.param("--x: ) f(!)", None, id="stray-then-bang"),
        pytest.param("--x: url(a)) b", None, id="after-a-url"),
        pytest.param("flex-wrap: wrap)", None, id="flex-wrap"),
        pytest.param("flex-wrap: f([)]) wrap", None, id="flex-wrap-nested"),
        pytest.param("width: calc(2px))", None, id="width"),
        pytest.param("--x: f([a])", ("--x", "f([a])", False), id="matched"),
        pytest.param("--x: f(\\))", ("--x", "f(\\))", False), id="escaped"),
        pytest.param("--x: [\\)]", ("--x", "[\\)]", False), id="escaped-in-square"),
        pytest.param("--x: f(\\29 )", ("--x", "f(\\29 )", False), id="hex"),
        pytest.param('--x: f(")")', ("--x", 'f(")")', False), id="string"),
        pytest.param("--x: url(a\\))", ("--x", "url(a\\))", False), id="url"),
        pytest.param("--x: f(/* ] */)", ("--x", "f( )", False), id="comment"),
    ],
)
def test_the_resolver_drops_a_closing_bracket_that_closes_nothing(
    declaration: str, read: tuple[str, str, bool] | None
) -> None:
    """Edge drops a declaration with a closing bracket that closes no open bracket, nested or
    not, `!` or none."""
    (rule,) = style_rules(f"p {{ {declaration}; color: #111111; }}")
    assert rule.declarations == (*((read,) if read else ()), ("color", "#111111", False))


@pytest.mark.parametrize(
    ("declaration", "read"),
    [
        pytest.param("--x: f([)", None, id="nested"),
        pytest.param("--x: f(] !x", None, id="paren-then-square"),
        pytest.param("--x: f([", ("--x", "f([", False), id="open"),
    ],
)
def test_the_resolver_drops_a_closing_bracket_that_closes_nothing_at_the_end(
    declaration: str, read: tuple[str, str, bool] | None
) -> None:
    """Edge drops `--x: f([)` at the end of the sheet and keeps `--x: f([` as written."""
    (rule,) = style_rules(f"p {{ color: #111111; {declaration}")
    assert rule.declarations == (("color", "#111111", False), *((read,) if read else ()))


@pytest.mark.parametrize(
    ("css", "page", "value"),
    [
        pytest.param(
            'p { flex-wrap: wrap; --x: a\\"; flex-wrap: nowrap; }',
            "<p>a</p>",
            "nowrap",
            id="double-quote",
        ),
        pytest.param(
            "p { flex-wrap: wrap; --x: a\\'; flex-wrap: nowrap; }",
            "<p>a</p>",
            "nowrap",
            id="single-quote",
        ),
        pytest.param(
            'p[title=a\\"b] { flex-wrap: wrap; }',
            "<p title='a\"b'>a</p>",
            "wrap",
            id="double-quote-in-a-selector",
        ),
        pytest.param(
            "p[title=a\\'b] { flex-wrap: wrap; }",
            '<p title="a\'b">a</p>',
            "wrap",
            id="single-quote-in-a-selector",
        ),
        pytest.param(
            'p[title=a\\"b] { flex-wrap: wrap; }', "<p>a</p>", None, id="selector-other-title"
        ),
        pytest.param(
            "p { flex-wrap: nowrap; --x: a\\/*; flex-wrap: wrap; }",
            "<p>a</p>",
            "wrap",
            id="slash-before-star",
        ),
        pytest.param(
            'p { flex-wrap: nowrap; --x: a\\\\"b;c"; flex-wrap: wrap; }',
            "<p>a</p>",
            "wrap",
            id="backslash-then-string",
        ),
        pytest.param(
            "p { flex-wrap: nowrap; --x: a\\\\/* b */; flex-wrap: wrap; }",
            "<p>a</p>",
            "wrap",
            id="backslash-then-comment",
        ),
        pytest.param(
            "p { flex-wrap: nowrap; --x: a\\3b flex-wrap: wrap; }",
            "<p>a</p>",
            "nowrap",
            id="hex-semicolon",
        ),
        pytest.param(
            "p { flex-wrap: nowrap; --x: a\\\\;flex-wrap:wrap; }",
            "<p>a</p>",
            "wrap",
            id="backslash-then-semicolon",
        ),
    ],
)
def test_the_resolver_reads_an_escape_outside_a_string_as_one_character(
    css: str, page: str, value: str | None
) -> None:
    """Edge reads an escaped quote or slash outside a string as text, so no string or comment
    opens there."""
    (paragraph,) = elements_of(page)
    assert value_of(read_sheet(css), paragraph, "flex-wrap", View(320)) == value


@pytest.mark.parametrize(
    ("css", "read"),
    [
        pytest.param(
            ':root { --x: a\\"b; --y: c; }',
            (("--x", 'a\\"b', False), ("--y", "c", False)),
            id="double-quote",
        ),
        pytest.param(
            ":root { --x: a\\'b; --y: c; }",
            (("--x", "a\\'b", False), ("--y", "c", False)),
            id="single-quote",
        ),
        pytest.param(
            ':root { --x: \\"; --y: c; }', (("--x", '\\"', False), ("--y", "c", False)), id="alone"
        ),
        pytest.param(
            ':root { --x: \\"\\"; --y: c; }',
            (("--x", '\\"\\"', False), ("--y", "c", False)),
            id="twice",
        ),
        pytest.param(
            ":root { --x: \\'\\\"; --y: c; }",
            (("--x", "\\'\\\"", False), ("--y", "c", False)),
            id="both-kinds",
        ),
        pytest.param(
            ':root { --x: \\61"b;c"; }', (("--x", '\\61"b;c"', False),), id="hex-then-string"
        ),
        pytest.param(
            ':root { --x: b !important; --x: a\\" !important; }',
            (("--x", "b", True), ("--x", 'a\\"', True)),
            id="important",
        ),
        pytest.param(
            ':root { --y: c; --x: a\\"',
            (("--y", "c", False), ("--x", 'a\\"', False)),
            id="at-the-end",
        ),
        pytest.param(
            ":root { --x: a\\/* b; --y: c; }",
            (("--x", "a\\/* b", False), ("--y", "c", False)),
            id="slash-before-star",
        ),
        pytest.param(
            ":root { --x: a/\\* b; --y: c; }",
            (("--x", "a/\\* b", False), ("--y", "c", False)),
            id="slash-then-escaped-star",
        ),
        pytest.param(':root { --x: "a\\"b"; }', (("--x", '"a\\"b"', False),), id="in-a-string"),
        pytest.param(':root { --x: url(a\\"b); }', (("--x", 'url(a\\"b)', False),), id="in-a-url"),
    ],
)
def test_the_resolver_keeps_an_escaped_quote_outside_a_string_as_written(
    css: str, read: tuple[tuple[str, str, bool], ...]
) -> None:
    """Edge keeps `--x: a\\"b` as written and reads the declaration after it."""
    (rule,) = style_rules(css)
    assert rule.declarations == read


@pytest.mark.parametrize(
    "css",
    [
        pytest.param(
            "p { flex-wrap: nowrap; } p { --x: a\\;flex-wrap:wrap;", id="semicolon-at-the-end"
        ),
        pytest.param("p { flex-wrap: nowrap; } p { --x: a\\;flex-wrap:wrap; }", id="semicolon"),
        pytest.param(":root { --x: a\\;b; }", id="semicolon-in-a-value"),
        pytest.param(
            "p { flex-wrap: nowrap; --x: f(a)\\; flex-wrap: wrap; }", id="after-a-bracket"
        ),
        pytest.param("p { flex-wrap: nowrap; --x: a\\} flex-wrap: wrap; }", id="closing-brace"),
        pytest.param("p { flex-wrap: nowrap; --x: a\\{ flex-wrap: wrap; }", id="opening-brace"),
        pytest.param(":root { --x: a\\}b; }", id="brace-in-a-value"),
        pytest.param("p\\{ { flex-wrap: wrap; }", id="brace-in-a-selector"),
        pytest.param(
            "p { flex-wrap: nowrap; } p { flex-wrap: wrap; --x: u\\72l(a b); }", id="url-name"
        ),
    ],
)
def test_the_resolver_refuses_an_escaped_semicolon_or_brace(css: str) -> None:
    """Edge reads an escaped `;` or brace as text, so `flex-wrap:wrap` after `a\\;` is part of
    `--x`, and an escaped `url` name as a `url()`; the reader refuses both rather than misread
    them."""
    with pytest.raises(UnreadCss):
        read_sheet(css)


@pytest.mark.parametrize(
    ("css", "value"),
    [
        pytest.param(':root { --x: \\61  url("a b"); }', '\\61  url("a b")', id="space"),
        pytest.param(':root { --x: \\61/**/url("a b"); }', '\\61 url("a b")', id="comment"),
        pytest.param(':root { --x: "\\61"url("a b"); }', '"\\61"url("a b")', id="string"),
    ],
)
def test_the_resolver_reads_a_url_name_apart_from_an_escape_before_it(css: str, value: str) -> None:
    """Edge reads `url("a b")` after `\\61 ` and a space, a comment or a string as its own
    name, so no escape joins it."""
    (rule,) = style_rules(css)
    assert rule.declarations == (("--x", value, False),)


@pytest.mark.parametrize(
    ("declaration", "read"),
    [
        pytest.param("--x: f(!important", ("--x", "f(!important", False), id="paren"),
        pytest.param("--x: [!important", ("--x", "[!important", False), id="square"),
        pytest.param("--x: f(a ! important", ("--x", "f(a ! important", False), id="spaced"),
        pytest.param("--x: f(!IMPORTANT", ("--x", "f(!IMPORTANT", False), id="capitals"),
        pytest.param("--x: f(g(a) !important", ("--x", "f(g(a) !important", False), id="nested"),
        pytest.param("--x: a( !important", ("--x", "a( !important", False), id="function"),
        pytest.param(
            "--x: \\61 ( !important", ("--x", "\\61 ( !important", False), id="escaped-name"
        ),
        pytest.param("--x: f(] !important", None, id="paren-then-square"),
        pytest.param("--x: [) !important", None, id="square-then-paren"),
        pytest.param("width: calc(2px !important", None, id="width-paren"),
        pytest.param("width: [2px !important", None, id="width-square"),
        pytest.param("width: calc(2px] !important", None, id="width-paren-then-square"),
        pytest.param("--x: f(a) !important", ("--x", "f(a)", True), id="closed-paren"),
        pytest.param("--x: [a] !important", ("--x", "[a]", True), id="closed-square"),
        pytest.param("--x: f(!important)", ("--x", "f(!important)", False), id="closed-around"),
        pytest.param("--x: a\\( !important", ("--x", "a\\(", True), id="escaped-paren"),
        pytest.param("--x: a\\[ !important", ("--x", "a\\[", True), id="escaped-square"),
        pytest.param("--x: \\( !important", ("--x", "\\(", True), id="escaped-paren-alone"),
        pytest.param("--x: a\\28  !important", ("--x", "a\\28", True), id="hex-paren"),
        pytest.param('--x: "(" !important', ("--x", '"("', True), id="string-paren"),
        pytest.param("--x: '[' !important", ("--x", "'['", True), id="string-square"),
        pytest.param("--x: /* ( */ a !important", ("--x", "a", True), id="comment-paren"),
        pytest.param("width: 1px !important", ("width", "1px", True), id="width-plain"),
    ],
)
def test_the_resolver_reads_importance_inside_a_bracket_open_at_the_end_as_text(
    declaration: str, read: tuple[str, str, bool] | None
) -> None:
    """Edge reads `!important` inside a bracket open at the end of the sheet as its text."""
    (rule,) = style_rules(f"p {{ flex-wrap: wrap; {declaration}")
    assert rule.declarations == (("flex-wrap", "wrap", False), *((read,) if read else ()))


@pytest.mark.parametrize("value", ["f(!important", "[!important"])
def test_the_resolver_keeps_a_whole_sheet_ending_inside_a_bracket_as_written(value: str) -> None:
    """Edge keeps `--x` as written, with no priority, when the sheet ends inside its bracket."""
    (rule,) = style_rules(f":root {{ --x: {value}")
    assert rule.declarations == (("--x", value, False),)


@pytest.mark.parametrize(
    ("css", "name", "value"),
    [
        ("p { width: 1px; } p { width: calc(2px !important", "width", "1px"),
        ("p { width: 1px !important; width: calc(2px !important", "width", "1px"),
        ("p { width: 1px !important; } p { width: calc(2px !important", "width", "1px"),
        ("p { width: 1px; } p { width: [2px !important", "width", "1px"),
        ("p { width: 1px; } p { width: calc(2px] !important", "width", "1px"),
        (
            "@media (min-width: 1px) { p { width: 1px !important; width: calc(2px !important",
            "width",
            "1px",
        ),
        ("p { width: 2px; width: 1px !important", "width", "1px"),
        ("p { flex-wrap: nowrap; } p { flex-wrap: f(!important", "flex-wrap", "nowrap"),
        ("p { flex-wrap: nowrap; } p { flex-wrap: wrap !important", "flex-wrap", "wrap"),
    ],
)
def test_the_resolver_weighs_importance_inside_a_bracket_open_at_the_end_as_a_browser_does(
    css: str, name: str, value: str
) -> None:
    """Edge drops `!important` inside a bracket open at the end, so an earlier value stays."""
    (paragraph,) = elements_of("<p>a</p>")
    assert value_of(read_sheet(css), paragraph, name, View(320)) == value


def test_the_resolver_refuses_importance_inside_a_substitution_open_at_the_end() -> None:
    """Edge drops `var(--w, 2px !important` at the end of the sheet, its `!` inside `var(`."""
    with pytest.raises(UnreadCss):
        read_sheet("p { width: 1px; } p { width: var(--w, 2px !important")


@pytest.mark.parametrize(
    "css",
    [
        "p { --x: var(--y, 2px !x); }",
        "p { --x: env(x, 2px !x); }",
        "p { --x: var(--y, a !important) !important; }",
        "p { --x: var(--y, 2px !important",
        "p { --x: VAR(--y, 2px !important",
        "p { --x: v\\61 r(--y, 2px !important",
        "p { --x: attr(x, 2px !important",
        "p { --x: --f(2px !important",
        "p { --x: if(x: 2px !important",
    ],
)
def test_the_resolver_refuses_a_bang_inside_a_substitution_of_a_custom_property(css: str) -> None:
    """Edge drops a custom property with a `!` straight inside a substitution."""
    with pytest.raises(UnreadCss):
        style_rules(css)


def test_the_resolver_keeps_a_bang_inside_a_string_or_a_url() -> None:
    """Edge keeps each of these declarations, and a string open at the end of the sheet is
    closed there, so the `!important` inside it is text."""
    rules = style_rules(
        'p { content: "!x"; flex-wrap: wrap; } p { content: "a!" !important; }'
        ' p { background: url(a!b); } p { flex-wrap: wrap; content: "a !important'
    )
    assert [rule.declarations for rule in rules] == [
        (("content", '"!x"', False), ("flex-wrap", "wrap", False)),
        (("content", '"a!"', True),),
        (("background", "url(a!b)", False),),
        (("flex-wrap", "wrap", False), ("content", '"a !important', False)),
    ]


def titled(title: str | None) -> str:
    return "<p>a</p>" if title is None else f'<p title="{escape(title)}">a</p>'


@pytest.mark.parametrize(
    ("page", "head", "matched"),
    [
        pytest.param(titled("\\"), '[title="\\\\"]', True, id="one-backslash"),
        pytest.param(titled("\\\\"), '[title="\\\\"]', False, id="two-backslashes"),
        pytest.param(titled("A"), '[title="\\41"]', True, id="hex"),
        pytest.param(titled("Ab"), '[title="\\000041b"]', True, id="hex-six-digits"),
        pytest.param(titled("ab"), '[title="\\a\\b"]', False, id="hex-letters"),
        pytest.param(titled('a"b'), '[title="a\\"b"]', True, id="escaped-double-quote"),
        pytest.param(titled("a'b"), "[title='a\\'b']", True, id="escaped-single-quote"),
        pytest.param(titled('"'), '[title="\\""]', True, id="only-an-escaped-quote"),
        pytest.param(titled("x"), "[title=x]", True, id="bare"),
        pytest.param(titled("A"), "[title=\\41]", True, id="bare-hex"),
        pytest.param(titled("\\"), "[title=\\\\]", True, id="bare-backslash"),
        pytest.param(titled(""), '[title=""]', True, id="empty"),
        pytest.param(titled(""), "[title='']", True, id="empty-single"),
        pytest.param("<p title>a</p>", '[title=""]', True, id="empty-on-a-bare-attribute"),
        pytest.param(titled(None), '[title=""]', False, id="empty-on-none"),
        pytest.param(titled("x"), "[title=\"x']", False, id="string-open-to-the-end"),
        pytest.param(titled("--x"), "[title=--x]", True, id="two-hyphens"),
        pytest.param(titled("--"), "[title=--]", True, id="two-hyphens-alone"),
        pytest.param(titled("--1"), "[title=--1]", True, id="two-hyphens-then-a-digit"),
        pytest.param(titled("---"), "[title=---]", True, id="three-hyphens"),
        pytest.param(titled("-x"), "[title=-x]", True, id="one-hyphen"),
    ],
)
def test_the_resolver_reads_an_attribute_selector_as_a_browser_reads_it(
    page: str, head: str, matched: bool
) -> None:
    (paragraph,) = elements_of(page)
    sheet = read_sheet(f"p{head} {{ flex-wrap: wrap; }}")
    assert (value_of(sheet, paragraph, "flex-wrap", View(320)) == "wrap") is matched


@pytest.mark.parametrize(
    "head", ["[title=]", "[title=1x]", "[title=x y]", "[title=], p", "[title=-1]", "[title=-]"]
)
def test_the_resolver_refuses_an_attribute_selector_a_browser_drops(head: str) -> None:
    with pytest.raises(UnreadCss):
        read_sheet(f"p{head} {{ flex-wrap: wrap; }}")


@pytest.mark.parametrize(
    "head", ['[title="\\41 b"]', '[title="a\\\nb"]', '[title="a b"]', '[ title = "x" ]']
)
def test_the_resolver_refuses_a_space_inside_an_attribute_selector(head: str) -> None:
    """Edge reads each of these; a selector is split at its spaces, so each is refused."""
    with pytest.raises(UnreadCss):
        read_sheet(f"p{head} {{ flex-wrap: wrap; }}")


def test_a_compound_reads_the_space_or_newline_an_escape_takes() -> None:
    """Edge reads `\\41 b` as `Ab` and an escaped newline in a string as nothing."""
    assert compound('[title="\\41 b"]').attributes == (("title", "Ab"),)
    assert compound('[title="a\\\nb"]').attributes == (("title", "ab"),)
    assert compound("[title=\\41 ]").attributes == (("title", "A"),)


@pytest.mark.parametrize("written", ["\\0", "\\d800", "\\110000"])
def test_a_compound_reads_an_escape_past_any_character_as_the_replacement(written: str) -> None:
    """Edge reads zero, a surrogate, and a number past U+10FFFF as U+FFFD."""
    assert compound(f'[title="{written}"]').attributes == (("title", "\ufffd"),)


@pytest.mark.parametrize(
    ("css", "name", "value"),
    [
        pytest.param('p { flex-wrap: wrap; content: "abc', "flex-wrap", "wrap", id="string"),
        pytest.param('p { content: "abc\n; flex-wrap: wrap; }', "flex-wrap", "wrap", id="newline"),
        pytest.param("p { content: 'abc\n; flex-wrap: wrap; }", "flex-wrap", "wrap", id="single"),
        pytest.param('p { content: "abc\r; flex-wrap: wrap; }', "flex-wrap", "wrap", id="return"),
        pytest.param('p { content: "abc\f; flex-wrap: wrap; }', "flex-wrap", "wrap", id="feed"),
        pytest.param(
            'p::after { content: "abc\n} p { flex-wrap: wrap; }',
            "flex-wrap",
            "wrap",
            id="newline-then-the-block-ends",
        ),
        pytest.param(
            'p { flex-wrap: wrap; } p { content: "a\n"; flex-wrap: nowrap; }',
            "flex-wrap",
            "wrap",
            id="newline-then-a-string-to-the-end",
        ),
        pytest.param(
            'p { flex-wrap: wrap; content: "abc\n }', "flex-wrap", "wrap", id="newline-keeps-before"
        ),
        pytest.param(
            'p { content: "abc\\\n; flex-wrap: nowrap; }"; flex-wrap: wrap; }',
            "flex-wrap",
            "wrap",
            id="escaped-newline",
        ),
        pytest.param("p { flex-wrap: wrap; ", "flex-wrap", "wrap", id="block"),
        pytest.param(
            "@media (min-width: 1px) { p { flex-wrap: wrap; ", "flex-wrap", "wrap", id="media"
        ),
        pytest.param(
            "@media (min-width: 1px) { p { flex-wrap: wrap; }",
            "flex-wrap",
            "wrap",
            id="media-alone",
        ),
        pytest.param("p { flex-wrap: wrap; background: url(abc", "flex-wrap", "wrap", id="url"),
        pytest.param("p { flex-wrap: wrap; } /* x", "flex-wrap", "wrap", id="comment"),
        pytest.param(
            "p { background: url(a b); flex-wrap: wrap; }"
            " p { flex-wrap: nowrap; background: url(c d; }",
            "flex-wrap",
            "nowrap",
            id="bad-url-to-the-end",
        ),
        pytest.param(
            'p { background: url(a"b;); flex-wrap: wrap; }', "flex-wrap", "wrap", id="bad-url-quote"
        ),
        pytest.param(
            "p { background: #ffeeee; } p { background: url(a b) #ffffff; }",
            "background",
            "#ffeeee",
            id="bad-url-dropped",
        ),
        pytest.param(
            'p { background: #ffeeee; } p { background: url(a"b) #ffffff; }',
            "background",
            "#ffeeee",
            id="bad-url-quote-dropped",
        ),
        pytest.param(
            'p { background: #ffeeee; } p { background: #ffffff "a\n; }',
            "background",
            "#ffeeee",
            id="bad-string-dropped",
        ),
        pytest.param(
            "p { background: #ffeeee; } p { background: url(a(b) #ffffff; }",
            "background",
            "#ffeeee",
            id="bad-url-bracket-dropped",
        ),
        pytest.param(
            "p { background: #ffeeee; } p { background: url(a\\\nb) #ffffff; }",
            "background",
            "#ffeeee",
            id="bad-url-escaped-newline-dropped",
        ),
        pytest.param(
            "p { background: #ffeeee; } p { background: url(a\\ b) #ffffff; }",
            "background",
            "url(a\\ b) #ffffff",
            id="url-escaped-space-kept",
        ),
        pytest.param(
            "p { background: #ffeeee; } p { background: url( a ) #ffffff; }",
            "background",
            "url( a ) #ffffff",
            id="url-spaces-at-the-ends-kept",
        ),
    ],
)
def test_the_resolver_reads_text_left_open_as_a_browser_reads_it(
    css: str, name: str, value: str
) -> None:
    """A string or `url(` open at the end of the sheet closes there, and so does a block; a
    string a newline ends, or a `url(` with a space, quote or bracket inside, drops the
    declaration that holds it, as Edge reads each."""
    (paragraph,) = elements_of("<p>a</p>")
    assert value_of(read_sheet(css), paragraph, name, View(320)) == value


@pytest.mark.parametrize(
    "css",
    [
        pytest.param("p { flex-wrap: nowrap; width: calc(1px; flex-wrap: wrap;", id="calc"),
        pytest.param('p { flex-wrap: nowrap; background: url("a"; flex-wrap: wrap;', id="url"),
        pytest.param(
            "p { flex-wrap: nowrap; background: url('a'; flex-wrap: wrap;", id="url-single"
        ),
        pytest.param(
            'p { flex-wrap: nowrap; background: url( "a"; flex-wrap: wrap;', id="url-space"
        ),
        pytest.param(
            "p {\r\n flex-wrap: nowrap;\r\n width: calc(1px;\r\n flex-wrap: wrap;", id="crlf"
        ),
        pytest.param("p { flex-wrap: nowrap; width: CALC(1px; flex-wrap: wrap;", id="capital"),
        pytest.param(
            'p { flex-wrap: nowrap; background: URL("a"; flex-wrap: wrap;', id="url-capital"
        ),
        pytest.param('p { flex-wrap: nowrap; width: url("x"; flex-wrap: wrap;', id="url-width"),
        pytest.param("p { flex-wrap: nowrap; color: var(--a; flex-wrap: wrap;", id="var"),
        pytest.param("p { flex-wrap: nowrap; --x: [a; flex-wrap: wrap;", id="square"),
        pytest.param(
            "p { flex-wrap: nowrap; width: calc(min(1px, 2px); flex-wrap: wrap;", id="outer"
        ),
        pytest.param("p { flex-wrap: nowrap; width: calc(min(1px; flex-wrap: wrap;", id="inner"),
        pytest.param(
            "@media (min-width: 1px) { p { flex-wrap: nowrap; width: calc(1px; flex-wrap: wrap;",
            id="media",
        ),
        pytest.param(
            "p { flex-wrap: nowrap; width: calc(1px; } p { flex-wrap: wrap; }", id="brace"
        ),
        pytest.param(
            "p { flex-wrap: nowrap; width: calc(1px } 2px); } p { flex-wrap: wrap; }",
            id="brace-then-closed",
        ),
        pytest.param(
            "p { flex-wrap: nowrap; } @font-face { src: local(a; } p { flex-wrap: wrap; }",
            id="font-face",
        ),
        pytest.param(
            "p { flex-wrap: nowrap; } @keyframes k { to { transform: scale(1; } }"
            " p { flex-wrap: wrap; }",
            id="keyframes",
        ),
        pytest.param(
            "p { flex-wrap: nowrap; width: calc(1px]; flex-wrap: wrap; }", id="other-closer"
        ),
        pytest.param(
            "p { flex-wrap: nowrap; --x: [a); flex-wrap: wrap; }", id="square-other-closer"
        ),
        pytest.param("p { color: #111111; width: calc(1px; color: #222222); }", id="closed-later"),
        pytest.param(
            "p { color: #111111; width: calc(1px\\; color: #222222); }", id="escaped-semicolon"
        ),
        pytest.param(
            "p { color: #111111; width: calc(1px\\} ); color: #222222; }", id="escaped-brace"
        ),
        pytest.param("p { flex-wrap: nowrap; } p:is(a { flex-wrap: wrap; }", id="selector"),
        pytest.param(
            "p { flex-wrap: nowrap; } @media (min-width: 1px { p { flex-wrap: wrap; } }",
            id="media-query",
        ),
    ],
)
def test_the_resolver_refuses_text_left_inside_a_bracket(css: str) -> None:
    """Edge reads a `;` or a brace inside an open bracket as part of it, up to its closing
    bracket or the end of the sheet, so `flex-wrap: wrap` after `calc(1px;` is never a
    declaration."""
    with pytest.raises(UnreadCss):
        read_sheet(css)


@pytest.mark.parametrize(
    ("css", "value"),
    [
        pytest.param("p { flex-wrap: nowrap; flex-wrap: wrap;", "wrap", id="block"),
        pytest.param('p { flex-wrap: nowrap; --x: "a; flex-wrap: wrap;', "nowrap", id="string"),
        pytest.param("p { flex-wrap: nowrap; --x: url(a; flex-wrap: wrap;", "nowrap", id="url"),
        pytest.param(
            'p { flex-wrap: nowrap; background: url("a; flex-wrap: wrap;', "nowrap", id="url-string"
        ),
        pytest.param("p { flex-wrap: wrap; --x: f(a", "wrap", id="open-at-the-end"),
        pytest.param("p { flex-wrap: wrap; --x: [a", "wrap", id="square-open-at-the-end"),
        pytest.param(
            "p { flex-wrap: nowrap; width: calc(1px); flex-wrap: wrap;", "wrap", id="calc"
        ),
        pytest.param(
            'p { flex-wrap: nowrap; background: url("a"); flex-wrap: wrap;', "wrap", id="quoted-url"
        ),
        pytest.param("p { flex-wrap: nowrap; --x: a\\(; flex-wrap: wrap; }", "wrap", id="escaped"),
        pytest.param("p { flex-wrap: nowrap; --x: a\\28 ; flex-wrap: wrap; }", "wrap", id="hex"),
        pytest.param('p { flex-wrap: nowrap; --x: "("; flex-wrap: wrap; }', "wrap", id="quoted"),
        pytest.param("p { flex-wrap: nowrap; --x: '['; flex-wrap: wrap; }", "wrap", id="single"),
        pytest.param("p { flex-wrap: nowrap; /* ( */ flex-wrap: wrap; }", "wrap", id="comment"),
        pytest.param(
            "p { flex-wrap: nowrap; --x: url(a(b); flex-wrap: wrap; }", "wrap", id="bad-url"
        ),
        pytest.param(
            "p { flex-wrap: nowrap; --x: url(a[b); flex-wrap: wrap; }", "wrap", id="in-url"
        ),
        pytest.param(
            "p { flex-wrap: nowrap; --x: a); flex-wrap: wrap; }", "wrap", id="stray-closer"
        ),
        pytest.param(
            "p { flex-wrap: nowrap; --x: f([a] (b)); flex-wrap: wrap; }", "wrap", id="nested"
        ),
        pytest.param(
            "p { flex-wrap: nowrap; } p:not(.a) { flex-wrap: wrap; }", "wrap", id="selector"
        ),
        pytest.param(
            "p { flex-wrap: nowrap; } @media (min-width: 1px) { p { flex-wrap: wrap; } }",
            "wrap",
            id="media-query",
        ),
    ],
)
def test_the_resolver_reads_a_closed_bracket_or_one_in_text_as_a_browser_reads_it(
    css: str, value: str
) -> None:
    """A bracket closed before the `;`, or one escaped, quoted, in a comment or in a `url()`,
    ends nothing, as Edge reads each."""
    (paragraph,) = elements_of("<p>a</p>")
    assert value_of(read_sheet(css), paragraph, "flex-wrap", View(320)) == value


@pytest.mark.parametrize(
    "tail",
    [
        ".places {flex-wrap:nowrap;--x:calc(1px;flex-wrap:wrap;",
        '.places {flex-wrap:nowrap;width:url("x";flex-wrap:wrap;',
    ],
)
def test_the_wrapping_check_refuses_a_wrap_left_inside_a_bracket(
    rendered: dict[str, str], tail: str
) -> None:
    """Edge keeps the page links on one line once this text ends the stylesheet."""
    page = rendered["parent, family"]
    assert links_wrap(read_sheet(stylesheet()), page, View(320))
    with pytest.raises(UnreadCss):
        links_wrap(read_sheet(f"{stylesheet()}\n{tail}"), page, View(320))


@pytest.mark.parametrize(
    "tail",
    [
        '.places { flex-wrap: wrap; --x: a\\"; flex-wrap: nowrap; }',
        ".places { flex-wrap: wrap; --x: a\\'; flex-wrap: nowrap; }",
    ],
)
def test_the_wrapping_check_reads_an_escaped_quote_as_text(
    rendered: dict[str, str], tail: str
) -> None:
    """Edge keeps the page links on one line once this text ends the stylesheet."""
    page = rendered["parent, family"]
    assert not links_wrap(read_sheet(f"{stylesheet()}\n{tail}"), page, View(320))


def test_the_wrapping_check_refuses_an_escaped_semicolon(rendered: dict[str, str]) -> None:
    """Edge reads `flex-wrap:wrap` after `a\\;` as part of `--x`, so the links stay on one
    line."""
    page = rendered["parent, family"]
    tail = ".places { flex-wrap: nowrap; } .places { --x: a\\;flex-wrap:wrap;"
    with pytest.raises(UnreadCss):
        links_wrap(read_sheet(f"{stylesheet()}\n{tail}"), page, View(320))


@pytest.mark.parametrize(
    "tail",
    [
        pytest.param("} p { }\n.places { flex-wrap: nowrap; }", id="stray-closing-brace"),
        pytest.param(
            ".places { flex-wrap: nowrap; } .places { flex-flow: wrap wrap; }", id="flex-flow"
        ),
    ],
)
def test_the_wrapping_check_reads_the_stylesheet_past_a_rule_a_browser_drops(
    rendered: dict[str, str], tail: str
) -> None:
    """Edge keeps the page links on one line once this text ends the stylesheet."""
    page = rendered["parent, family"]
    assert links_wrap(read_sheet(stylesheet()), page, View(320))
    assert not links_wrap(read_sheet(f"{stylesheet()}\n{tail}"), page, View(320))


@pytest.mark.parametrize(
    ("inside", "kept"),
    [
        pytest.param("a\\41 b", True, id="hex-space"),
        pytest.param("a\\41\tb", True, id="hex-tab"),
        pytest.param("a\\41\nb", True, id="hex-newline"),
        pytest.param("a\\41\rb", True, id="hex-return"),
        pytest.param("a\\41\fb", True, id="hex-feed"),
        pytest.param("a\\41\r\nb", True, id="hex-crlf"),
        pytest.param("a\\4A b", True, id="hex-capital"),
        pytest.param("a\\000041 b", True, id="hex-six-digits"),
        pytest.param("a\\41 ", True, id="hex-space-at-the-end"),
        pytest.param('"a\\\r\nb"', True, id="quoted-escaped-crlf"),
        pytest.param("a\\41  b", False, id="hex-two-spaces"),
        pytest.param("a\\0000041 b", False, id="hex-seven-digits"),
        pytest.param('a\\41 "b', False, id="hex-space-then-quote"),
        pytest.param("a\\41 (b", False, id="hex-space-then-bracket"),
        pytest.param("a\\41\r\n b", False, id="hex-crlf-then-space"),
        pytest.param("a\\\r\nb", False, id="escaped-crlf"),
        pytest.param("a\\\n", False, id="escaped-newline-at-the-end"),
        pytest.param("a\\\r\n", False, id="escaped-crlf-at-the-end"),
        pytest.param("a\\\f", False, id="escaped-feed-at-the-end"),
        pytest.param("a\\\n ", False, id="escaped-newline-then-space"),
    ],
)
def test_the_resolver_reads_a_hex_escape_in_a_url_as_a_browser_reads_it(
    inside: str, kept: bool
) -> None:
    """Edge reads a hex escape in a `url()` with the one space after it, a CRLF being one,
    and drops a `url()` with a backslash before a newline."""
    (paragraph,) = elements_of("<p>a</p>")
    written = f"url({inside}) #ffffff"
    sheet = read_sheet(f"p {{ background: #ffeeee; }} p {{ background: {written}; }}")
    expected = written.lower() if kept else "#ffeeee"
    assert value_of(sheet, paragraph, "background", View(320)) == expected


@pytest.mark.parametrize("quote", ['"', "'"])
@pytest.mark.parametrize(
    ("cut", "value"),
    [
        pytest.param("\\\r\n", "wrap", id="escaped-crlf"),
        pytest.param("\\\r", "wrap", id="escaped-return"),
        pytest.param("\\\f", "wrap", id="escaped-feed"),
        pytest.param("\\41\n", "wrap", id="hex-newline"),
        pytest.param("\\41\r\n", "wrap", id="hex-crlf"),
        pytest.param("\\41\r", "wrap", id="hex-return"),
        pytest.param("\\41\f", "wrap", id="hex-feed"),
        pytest.param("\\000041\n", "wrap", id="hex-six-digits-newline"),
        pytest.param("\\0000041\n", "nowrap", id="hex-seven-digits-newline"),
        pytest.param("\\41\t\n", "nowrap", id="hex-tab-then-newline"),
        pytest.param("\\41\r\n\n", "nowrap", id="hex-crlf-then-newline"),
        pytest.param("\r\n", "nowrap", id="crlf"),
    ],
)
def test_the_resolver_reads_an_escaped_newline_in_a_string_as_a_browser_reads_it(
    quote: str, cut: str, value: str
) -> None:
    """Edge reads an escaped CRLF, and the newline a hex escape takes, as part of the
    string, so the string goes on to its closing quote."""
    (paragraph,) = elements_of("<p>a</p>")
    css = f"p {{ content: {quote}abc{cut}; flex-wrap: nowrap; }}{quote}; flex-wrap: wrap; }}"
    assert value_of(read_sheet(css), paragraph, "flex-wrap", View(320)) == value


def test_the_resolver_refuses_a_selector_a_newline_cuts_a_string_of() -> None:
    """Edge drops the rule, its selector holding a string a newline ends."""
    with pytest.raises(UnreadCss):
        read_sheet('p { flex-wrap: nowrap; } p[title="a\n], p { flex-wrap: wrap; }')


def test_the_resolver_reads_links_strings_and_keywords_the_way_a_browser_would() -> None:
    page = elements_of('<section><p class="x">a</p><a class="x" href="/">b</a><a class="x">c</a>')
    _, para, link, anchor = page
    phone = View(320)
    links = read_sheet(
        """
        .x:link { max-width: 1px; }
        section .x { max-width: 2px; }
        a.x:visited { max-width: 3px; }
        """
    )
    assert value_of(links, para, "max-width", phone) == "2px"
    assert value_of(links, link, "max-width", phone) == "1px"
    assert value_of(links, anchor, "max-width", phone) == "2px"
    assert compound("a:LINK").specificity == (0, 1, 1)
    twice = read_sheet("p.x { max-width: 6px; } .x.x { max-width: 5px; } .x { max-width: 4px; }")
    assert value_of(twice, para, "max-width", phone) == "5px"
    assert compound(".x.x").specificity == (0, 2, 0)
    rows = elements_of("<ol><li>1</li><li>2</li><li>3</li></ol>")
    places = read_sheet("li:first-child { max-width: 1px; } li:nth-child(2) { max-width: 2px; }")
    assert [value_of(places, one, "max-width", phone) for one in rows[1:]] == ["1px", "2px", None]
    assert compound("li:nth-child(2)").specificity == (0, 1, 1)
    strings = read_sheet(
        """
        p::before { content: "} /* ;"; }
        p { background: url(a;b/*c.png); flex-wrap: wrap; }
        p::after { content: '*/ {'; }
        p { width: 7px; }
        """
    )
    assert value_of(strings, para, "flex-wrap", phone) == "wrap"
    assert value_of(strings, para, "width", phone) == "7px"
    flow = read_sheet(
        "p { flex-wrap: wrap; flex-direction: column; } .x { flex-flow: row-reverse; }"
    )
    assert value_of(flow, para, "flex-wrap", phone) == "nowrap"
    assert value_of(flow, para, "flex-direction", phone) == "row-reverse"
    keywords = read_sheet(
        """
        section { flex-wrap: wrap; overflow-wrap: anywhere; white-space: nowrap; }
        p { flex-wrap: inherit; overflow-wrap: unset; white-space: initial; }
        a { flex-wrap: unset; }
        """
    )
    assert value_of(keywords, para, "flex-wrap", phone) == "wrap"
    assert value_of(keywords, para, "overflow-wrap", phone) == "anywhere"
    assert value_of(keywords, para, "white-space", phone) is None
    assert value_of(keywords, link, "flex-wrap", phone) is None
    with pytest.raises(UnreadCss):
        value_of(read_sheet("p { flex-wrap: revert; }"), para, "flex-wrap", phone)


def test_the_resolver_reads_each_side_of_a_box_and_the_shrinking_of_a_flex_item() -> None:
    _, bold, italic = elements_of("<div><b>1</b><i>2</i></div>")
    phone = View(320, 16, 32)
    boxes = read_sheet(
        """
        b { padding: 1px 2px 3px 4px; margin: 0 auto; border: 3px solid red; }
        b { padding-inline-end: 9px; flex: none; }
        i { padding-inline: 5px 1rem; border-left: thin dashed; border-right: none; }
        i { margin-inline-start: min(2vw, 1rem); border-inline-width: 0 2px; flex: 2; }
        """
    )
    assert [value_of(boxes, bold, name, phone) for name in INSETS] == (
        ["auto", "auto", "3px", "3px", "4px", "9px"]
    )
    assert [value_of(boxes, italic, name, phone) for name in INSETS] == (
        ["min(2vw, 1rem)", None, "0", "2px", "5px", "1rem"]
    )
    assert value_of(boxes, bold, "flex-shrink", phone) == "0"
    assert value_of(boxes, italic, "flex-shrink", phone) == "1"
    assert inset_px("min(2vw, 1rem)", phone) == 6.4
    assert inset_px("min(2vw, 1rem)", View(1440)) == 16
    assert inset_px("1.5em", phone) == 48
    assert [inset_px(one, phone) for one in (None, "auto", "0", "thin", "medium", "thick")] == (
        [0, 0, 0, 1, 3, 5]
    )
    for unread in ("10%", "calc(1px + 1px)", "1ch"):
        with pytest.raises(UnreadCss):
            inset_px(unread, phone)
    for unread in ("p { border-style: none; }", "p { padding: 1px 2px 3px 4px 5px; }"):
        with pytest.raises(UnreadCss):
            read_sheet(unread)


@pytest.mark.parametrize("order", [("rendered", "opened"), ("opened", "rendered")], ids="-".join)
def test_both_sets_of_pages_render_for_one_test_in_either_order(
    order: tuple[str, str], request: pytest.FixtureRequest, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A test that asks for both sets of pages before either is kept still runs on its own."""
    monkeypatch.setattr(sys.modules[__name__], "RENDERED", {})
    monkeypatch.setattr(sys.modules[__name__], "OPENED", {})
    pages = {name: request.getfixturevalue(name) for name in order}
    assert len(pages["rendered"]) == 6
    assert len(pages["opened"]) == 4


@pytest.mark.parametrize("view", ROW_VIEWS, ids=str)
def test_the_page_links_and_sign_out_wrap_onto_lines_of_their_own(
    rendered: dict[str, str], view: View
) -> None:
    """For her, a signed-in parent, and with the sign-in off, on every screen: the links and
    the sign-out control wrap rather than run past the edge. Just above the narrow layout
    with the page's text at 200%, the brand and the links don't fit one row, so the masthead
    wraps them onto a line of their own; where everything fits one line, nothing moves."""
    sheet = read_sheet(stylesheet())
    for name, page in with_nav(rendered).items():
        assert links_wrap(sheet, page, view), name


@pytest.mark.parametrize("view", VIEWS, ids=str)
def test_the_mark_and_wordmark_wrap_only_where_the_masthead_is_a_column(
    rendered: dict[str, str], view: View
) -> None:
    """On every page, the sign-in page included, and on a phone at every text size the
    masthead is a column whose brand wraps within the line; on a wide screen it is a row
    whose brand keeps one line."""
    sheet = read_sheet(stylesheet())
    for name, page in rendered.items():
        head = masthead_of(page)[0]
        if view.width == 320:
            assert value_of(sheet, head, "flex-direction", view) == "column", name
        assert brand_wraps_where_it_should(sheet, page, view), name


@pytest.mark.parametrize("view", VIEWS, ids=str)
def test_the_entry_columns_are_never_wider_than_the_form(
    rendered: dict[str, str], view: View
) -> None:
    """On the family page, for a signed-in parent and with the sign-in off."""
    sheet = read_sheet(stylesheet())
    for name, page in family(rendered).items():
        assert entry_columns_fit(sheet, page, view), name


@pytest.mark.parametrize("view", VIEWS, ids=str)
def test_add_assignments_labels_and_buttons_break_a_word_only_when_it_cannot_fit(
    rendered: dict[str, str], opened: dict[str, str], view: View
) -> None:
    """The summary that opens the fold, and both forms, the pasted text's and the one
    assignment's, with the fold closed and as a refused form returns it open."""
    sheet = read_sheet(stylesheet())
    for name, page in (family(rendered) | opened).items():
        assert words_break_where_they_must(sheet, page, view) == [], name


def test_nothing_in_the_masthead_or_the_forms_is_cut_or_shrunk_on_any_screen(
    rendered: dict[str, str], opened: dict[str, str]
) -> None:
    """No element of the masthead, or of Add assignments' summary and forms, is held to one
    line, cut off, or given a smaller text size on a narrow screen, and nothing above them
    hides what runs past it, with the fold closed or open."""
    sheet = read_sheet(stylesheet())
    for name, page in (rendered | opened).items():
        held = masthead_of(page) + (entry_surface_of(page) if name.endswith("family") else [])
        assert cut_or_shrunk(sheet, held) == [], name


# ------------------------------------------------------------- and each fails when broken


PLACES_RULE = """.places {
  display: flex;
  flex-wrap: wrap;
  gap: 0.35rem;
}"""
BRAND_RULE = """  .brand {
    flex-wrap: wrap;
  }"""
BRAND_BASE = """.brand {
  display: inline-flex;
  align-items: center;
  min-height: 2.75rem;
  gap: 0.65rem;
  text-decoration: none;
}"""
ENTRY_COLUMNS = "grid-template-columns: repeat(auto-fit, minmax(min(13rem, 100%), 1fr));"
ADDING_RULE = """#add-assignments > summary,
#add-assignments label,
#add-assignments button {
  overflow-wrap: anywhere;
}"""


def broken(was: str, becomes: str) -> Sheet:
    css = stylesheet()
    assert css.count(was) == 1, was
    return read_sheet(css.replace(was, becomes))


@pytest.mark.parametrize(
    ("becomes", "wraps_up_to"),
    [
        pytest.param(PLACES_RULE.replace("  flex-wrap: wrap;\n", ""), 0, id="no-wrap"),
        pytest.param(PLACES_RULE.replace("wrap;", "nowrap;"), 0, id="nowrap"),
        pytest.param(PLACES_RULE.replace(".places {", ".place {"), 0, id="selector-broken"),
        pytest.param(
            f"{PLACES_RULE}\n@media (min-width: 30rem) {{ .places {{ flex-wrap: nowrap; }} }}",
            479,
            id="undone-on-wider-screens",
        ),
    ],
)
def test_the_links_check_fails_when_the_rule_stops_wrapping(
    rendered: dict[str, str], becomes: str, wraps_up_to: int
) -> None:
    sheet = broken(PLACES_RULE, becomes)
    for page in with_nav(rendered).values():
        for view in VIEWS:
            expected = view.width * 16 <= wraps_up_to * view.browser_text
            assert links_wrap(sheet, page, view) == expected, view


@pytest.mark.parametrize(
    ("added", "fails_in"),
    [
        pytest.param("width: max-content;", {"column"}, id="max-content-wide"),
        pytest.param("inline-size: 60rem;", {"column"}, id="inline-size-past-the-line"),
        pytest.param("min-width: max-content;", {"column", "row"}, id="min-width-max-content"),
        pytest.param("min-width: 40rem;", {"column", "row"}, id="fixed-min-width"),
        pytest.param("flex-shrink: 0;", {"row"}, id="no-shrink"),
        pytest.param("flex: none;", {"row"}, id="flex-none"),
        pytest.param("flex: 1 0 auto;", {"row"}, id="flex-without-shrink"),
        pytest.param("width: 100%;", set(), id="the-line-wide"),
        pytest.param("width: fit-content; min-width: 0;", set(), id="fit-content"),
        pytest.param("flex: 1 1 auto; min-width: min-content;", set(), id="shrinks"),
    ],
)
def test_the_links_check_fails_where_the_links_are_held_wider_than_the_line(
    rendered: dict[str, str], added: str, fails_in: set[str]
) -> None:
    sheet = read_sheet(f"{stylesheet()}\n.places {{ {added} }}\n")
    for page in with_nav(rendered).values():
        head = masthead_of(page)[0]
        for view in VIEWS:
            column = value_of(sheet, head, "flex-direction", view) == "column"
            layout = "column" if column else "row"
            assert links_wrap(sheet, page, view) == (layout not in fails_in), view


DATE_VIEWS = [
    View(width, browser, root)
    for width in (
        320,
        330,
        340,
        360,
        390,
        420,
        480,
        481,
        500,
        540,
        600,
        768,
        820,
        960,
        1180,
        1440,
        3840,
    )
    for browser, root in TEXT_SIZES.values()
]
DATES_RULE = """  #add-assignments > .panel {
    margin-inline: min(0px, 4vw - 1.25rem);
    padding-inline: 0;
    border: 0;
    border-radius: 0;
    background: none;
    box-shadow: none;
    backdrop-filter: none;
  }

  #add-assignments input,
  #add-assignments select,
  #add-assignments textarea {
    padding-inline: min(0.85rem, 2vw);
  }"""


@pytest.mark.parametrize("view", DATE_VIEWS, ids=str)
def test_a_date_field_has_room_for_its_text_and_button(
    rendered: dict[str, str], view: View
) -> None:
    sheet = read_sheet(stylesheet())
    for name, page in family(rendered).items():
        assert min(date_room(sheet, page, view)) >= 0, name


@pytest.mark.parametrize(
    ("text", "typed", "spaced"),
    [(16, 120.64, 139.83), (24, 175.95, 204.72), (32, 231.22, 269.61)],
)
def test_the_date_room_leaves_what_edge_draws_at_each_text_size(
    text: int, typed: float, spaced: float
) -> None:
    """Edge's widths for 04/04/0000 with the calendar button, with and without a reader's
    letter spacing, each four widened to a zero (20.99 and 19.14 px at 32 px text)."""
    zeros = 2 * (20.99 - 19.14) * text / 32
    assert typed + zeros <= DATE_EM * text + DATE_PX <= typed + zeros + 0.5
    need = (DATE_EM + READER_SPACING_EM) * text + DATE_PX
    assert spaced + zeros <= need <= spaced + zeros + 0.5


@pytest.mark.parametrize("view", DATE_VIEWS, ids=str)
def test_the_brand_fits_its_line_with_a_readers_letter_spacing_on_every_screen(
    rendered: dict[str, str], view: View
) -> None:
    sheet = read_sheet(stylesheet())
    for name, page in rendered.items():
        assert brand_wraps_where_it_should(sheet, page, view), name


@pytest.mark.parametrize(
    ("text", "own", "spaced"), [(28, 117.25, 134.89), (42, 175.86, 202.33), (56, 234.48, 269.77)]
)
def test_the_wordmark_model_leaves_what_edge_draws_at_each_text_size(
    text: int, own: float, spaced: float
) -> None:
    """Edge's widths for the wordmark with its own 0.03em letter spacing and a reader's."""
    assert own <= text * (WORDMARK_EM + len(WORDMARK) * 0.03) <= own + 0.5
    need = text * (WORDMARK_EM + len(WORDMARK) * READER_LETTER_SPACING_EM)
    assert spaced <= need <= spaced + 0.5


MASTHEAD_PADDING = "    padding-inline: min(1.25rem, 6.25vw);\n"


@pytest.mark.parametrize(
    ("was", "becomes", "view", "holds"),
    [
        pytest.param(MASTHEAD_PADDING, "", View(320, 16, 32), False, id="kept-page-text"),
        pytest.param(MASTHEAD_PADDING, "", View(320, 32, 32), False, id="kept-browser-text"),
        pytest.param(MASTHEAD_PADDING, "", View(349, 16, 32), False, id="kept-349"),
        pytest.param(MASTHEAD_PADDING, "", View(350, 16, 32), True, id="kept-350"),
        pytest.param(MASTHEAD_PADDING, "", View(320, 16, 16), True, id="kept-ordinary-text"),
        pytest.param("6.25vw", "7.84vw", View(320, 16, 32), True, id="share-at-the-fit"),
        pytest.param("6.25vw", "7.85vw", View(320, 16, 32), False, id="share-past-the-fit"),
        pytest.param("6.25vw", "7.85vw", View(320, 32, 32), False, id="share-past-browser-text"),
    ],
)
def test_the_brand_check_fails_where_the_masthead_leaves_the_wordmark_too_short_a_line(
    rendered: dict[str, str], was: str, becomes: str, view: View, holds: bool
) -> None:
    """With large text and a reader's letter spacing, the wordmark is about 270 px wide."""
    sheet = broken(was, becomes)
    for page in rendered.values():
        assert brand_wraps_where_it_should(sheet, page, view) is holds


@pytest.mark.parametrize(
    ("added", "view", "holds"),
    [
        pytest.param(".wordmark { letter-spacing: 0.146em; }", View(320, 16, 32), True, id="own"),
        pytest.param(".wordmark { letter-spacing: 0.147em; }", View(320, 16, 32), False, id="wide"),
        pytest.param(".wordmark { letter-spacing: 9.4px; }", View(320, 16, 32), False, id="px"),
        pytest.param(".wordmark { letter-spacing: 0.3rem; }", View(320, 16, 32), False, id="rem"),
        pytest.param(".wordmark { letter-spacing: -1em; }", View(320, 16, 32), True, id="tight"),
        pytest.param(".wordmark { font-size: 1.8rem; }", View(320, 16, 32), True, id="text"),
        pytest.param(".wordmark { font-size: 1.82rem; }", View(320, 16, 32), False, id="big"),
        pytest.param(".masthead { border-inline: 5px solid; }", View(320, 16, 32), True, id="b5"),
        pytest.param(".masthead { border-inline: 6px solid; }", View(320, 16, 32), False, id="b6"),
        pytest.param(".masthead { margin-inline: 5px; }", View(320, 16, 32), True, id="m5"),
        pytest.param(".masthead { margin-inline: 6px; }", View(320, 16, 32), False, id="m6"),
        pytest.param(".masthead { max-width: 309.81px; }", View(320, 16, 32), True, id="most"),
        pytest.param(".masthead { max-width: 309.8px; }", View(320, 16, 32), False, id="less"),
        pytest.param(".mark { width: 280px; }", View(320, 16, 32), True, id="mark"),
        pytest.param(".mark { width: 280.01px; }", View(320, 16, 32), False, id="mark-wide"),
        pytest.param(".brand { gap: 2.59rem; }", View(481, 16, 32), True, id="row-gap"),
        pytest.param(".brand { gap: 2.6rem; }", View(481, 16, 32), False, id="row-wide-gap"),
        pytest.param(".brand { column-gap: 2.6rem; }", View(481, 16, 32), False, id="column-gap"),
        pytest.param(".brand { gap: 0 2.6rem; }", View(481, 16, 32), False, id="gap-pair"),
        pytest.param(".brand { gap: 2.6rem 0; }", View(481, 16, 32), True, id="row-gap-only"),
        pytest.param(".brand { gap: normal; }", View(481, 16, 32), True, id="normal"),
        pytest.param(".brand { gap: 0; }", View(481, 16, 32), True, id="no-gap"),
        pytest.param(".wordmark { letter-spacing: 0; }", View(320, 16, 32), True, id="unspaced"),
        pytest.param(".wordmark { letter-spacing: 6.72px; }", View(320, 16, 32), True, id="px-ok"),
        pytest.param(
            ".wordmark { letter-spacing: 0.21rem; }", View(320, 16, 32), True, id="rem-ok"
        ),
        pytest.param(
            ".wordmark { padding-inline: 4.59px; } .brand { border-inline: 0.5px solid; }",
            View(320, 16, 32),
            False,
            id="hairline-border-drawn-whole",
        ),
        pytest.param(".brand { gap: 2.6rem; }", View(320, 16, 32), True, id="gap-in-a-column"),
        pytest.param(".brand { letter-spacing: 0.3em; }", View(320, 16, 32), True, id="own-wins"),
        pytest.param(
            ".wordmark { letter-spacing: inherit; } .brand { letter-spacing: 0.255em; }",
            View(320, 16, 32),
            True,
            id="inherited-ems-of-the-root",
        ),
        pytest.param(
            ".wordmark { letter-spacing: inherit; } .brand { letter-spacing: 0.256em; }",
            View(320, 16, 32),
            False,
            id="inherited-past-the-fit",
        ),
        pytest.param(
            ".wordmark { letter-spacing: unset; } .masthead { letter-spacing: 0.15em; }",
            View(320, 16, 32),
            True,
            id="unset-from-the-masthead",
        ),
        pytest.param(".wordmark { padding-inline: 5px; }", View(320, 16, 32), True, id="wp5"),
        pytest.param(".wordmark { padding-inline: 6px; }", View(320, 16, 32), False, id="wp6"),
        pytest.param(".wordmark { margin-left: 10.19px; }", View(320, 16, 32), True, id="wm"),
        pytest.param(".wordmark { margin-left: 10.2px; }", View(320, 16, 32), False, id="wm-past"),
        pytest.param(".brand { border-inline: 5px solid; }", View(320, 16, 32), True, id="bb5"),
        pytest.param(".brand { margin-inline: 6px; }", View(320, 16, 32), False, id="bm6"),
        pytest.param(".brand { padding-inline: 0.95rem; }", View(481, 16, 32), True, id="row-bp"),
        pytest.param(".brand { padding-inline: 1rem; }", View(481, 16, 32), False, id="row-bp-1"),
        pytest.param(".mark { margin-inline: 116px; }", View(320, 16, 32), True, id="mm"),
        pytest.param(".mark { margin-inline: 116.01px; }", View(320, 16, 32), False, id="mm-past"),
        pytest.param(".mark { min-width: 280px; }", View(320, 16, 32), True, id="mark-least"),
        pytest.param(".mark { min-width: 280.01px; }", View(320, 16, 32), False, id="least-past"),
        pytest.param(".masthead { zoom: 1; }", View(320, 16, 32), True, id="no-zoom"),
        pytest.param(
            ".elsewhere { gap: max(1rem, calc(1vw + 2px)); }",
            View(481, 16, 32),
            True,
            id="nested-gap-elsewhere",
        ),
    ],
)
def test_the_brand_check_reads_the_wordmark_the_mark_and_the_line_from_the_cascade(
    rendered: dict[str, str], added: str, view: View, holds: bool
) -> None:
    """At 320 px the line is 280 px; just above the narrow layout it is 401 px for the mark,
    the gap and the wordmark side by side."""
    rule = NARROW % added if view.width == 320 else added
    sheet = read_sheet(stylesheet() + "\n" + rule + "\n")
    for page in rendered.values():
        assert brand_wraps_where_it_should(sheet, page, view) is holds


@pytest.mark.parametrize(
    "added",
    [
        ".wordmark { letter-spacing: calc(0.1em + 1px); }",
        ".wordmark { font-size: 120%; }",
        ".mark { width: auto; }",
        ".brand { gap: 1rem 2rem 3rem; }",
        ".brand { gap: max(1rem, calc(1vw + 2px)); }",
        ".brand { font-size: 1.5rem; }",
        ".masthead { font: 600 1rem serif; }",
        ".wordmark { zoom: 1.2; }",
        ".brand { zoom: 1.2; }",
        ".mark { zoom: 0.5; }",
    ],
)
def test_the_brand_check_refuses_what_it_does_not_read(
    rendered: dict[str, str], added: str
) -> None:
    with pytest.raises(UnreadCss):
        brand_wraps_where_it_should(
            read_sheet(stylesheet() + "\n" + added + "\n"),
            rendered["sign-in page"],
            View(320, 16, 32),
        )


@pytest.mark.parametrize("view", DATE_VIEWS, ids=str)
def test_the_masthead_keeps_its_padding_wherever_a_share_of_the_screen_allows_it(
    rendered: dict[str, str], view: View
) -> None:
    """The masthead's side padding is its ordinary 1.25rem, held in the narrow layout to
    6.25% of the screen's width, so ordinary text from 320 px up keeps it."""
    sheet = read_sheet(stylesheet())
    head = masthead_of(rendered["sign-in page"])[0]
    sides = [padding_px(value_of(sheet, head, side, view), view) for side in INSETS[4:]]
    ordinary = 1.25 * view.root_text
    expected = min(ordinary, 0.0625 * view.width) if column(sheet, head, view) else ordinary
    assert sides == [pytest.approx(expected)] * 2
    if view.root_text == 16 and view.width >= 320:
        assert sides == [ordinary] * 2


CARD_EDGES = {
    **dict.fromkeys(
        ("border-top-width", "border-right-width", "border-bottom-width", "border-left-width"), "0"
    ),
    **dict.fromkeys(CORNERS, "0"),
    "background": "none",
    "background-color": "transparent",
    "background-image": "none",
    "box-shadow": "none",
    "backdrop-filter": "none",
}


def card_is_bare(sheet: Sheet, page: str, view: View) -> tuple[bool, bool]:
    """Whether the Add assignments card gives up its side insets on ``view``, and whether it
    gives up its edge, surface and shadow with them."""
    card = [
        one
        for one in elements_of(page)
        if "panel" in one.classes
        and one.parent is not None
        and one.parent.attributes.get("id") == "add-assignments"
    ]
    assert len(card) == 1
    insets = [value_of(sheet, card[0], side, view) for side in ("padding-left", "padding-right")]
    edges = [value_of(sheet, card[0], name, view) for name in CARD_EDGES]
    return insets == ["0", "0"], edges == list(CARD_EDGES.values())


@pytest.mark.parametrize("view", VIEWS, ids=str)
def test_the_add_assignments_card_drops_its_edge_with_its_insets(
    rendered: dict[str, str], view: View
) -> None:
    sheet = read_sheet(stylesheet())
    for page in family(rendered).values():
        bare, edgeless = card_is_bare(sheet, page, view)
        assert bare == edgeless
        assert bare == (view.width <= 30 * view.browser_text)


def with_line(after: str, line: str) -> str:
    return DATES_RULE.replace(f"    {after}\n", f"    {after}\n    {line}\n")


@pytest.mark.parametrize(
    "becomes",
    [
        pytest.param(DATES_RULE.replace("    border: 0;\n", ""), id="border"),
        pytest.param(DATES_RULE.replace("border: 0;", "border-inline: 0;"), id="sides-only"),
        pytest.param(
            DATES_RULE.replace("border: 0;", "border-left: 0;\n    border-right: 0;"),
            id="left-and-right",
        ),
        pytest.param(with_line("border: 0;", "border-top: 1px solid red;"), id="top"),
        pytest.param(DATES_RULE.replace("    border-radius: 0;\n", ""), id="radius"),
        pytest.param(with_line("border-radius: 0;", "border-top-left-radius: 12px;"), id="corner"),
        pytest.param(DATES_RULE.replace("    background: none;\n", ""), id="background"),
        pytest.param(with_line("background: none;", "background-color: #fff;"), id="fill"),
        pytest.param(
            with_line("background: none;", "background-image: linear-gradient(red, blue);"),
            id="image",
        ),
        pytest.param(DATES_RULE.replace("    box-shadow: none;\n", ""), id="shadow"),
        pytest.param(DATES_RULE.replace("    backdrop-filter: none;\n", ""), id="backdrop"),
    ],
)
def test_the_card_check_fails_when_the_card_keeps_part_of_its_edge(
    rendered: dict[str, str], becomes: str
) -> None:
    sheet = broken(DATES_RULE, becomes)
    for page in family(rendered).values():
        assert card_is_bare(sheet, page, View(320)) == (True, False)


@pytest.mark.parametrize(
    ("added", "room"),
    [
        pytest.param("#add-assignments > .panel { border: 10vw solid red; }", -57.744, id="vw"),
        pytest.param(
            "#add-assignments > .panel { border-left: 10vw solid; border-right: 10vw solid; }",
            -57.744,
            id="vw-sides",
        ),
        pytest.param(
            "#add-assignments > .panel { border: min(10vw, 1rem) solid; }", -57.744, id="min"
        ),
        pytest.param(
            "#add-assignments { border: 3px solid; }"
            " #add-assignments > .panel { border: inherit; }",
            -5.744,
            id="inherit",
        ),
        pytest.param("#add-assignments input { border: 10vw solid red; }", -55.744, id="field-vw"),
        pytest.param("#add-assignments > .panel { border: 0.5px solid; }", 4.256, id="half-px"),
        pytest.param("#add-assignments > .panel { border: 0.2vw solid; }", 4.256, id="under-px-vw"),
        pytest.param("#add-assignments > .panel { border: 1.9px solid; }", 4.256, id="floored"),
        pytest.param("#add-assignments > .panel { border: 2.5px solid; }", 2.256, id="floored-2"),
        pytest.param("#add-assignments input { border: 0.5px solid; }", 6.256, id="field-half-px"),
        pytest.param("#add-assignments input { border: 2.99px solid; }", 4.256, id="field-floored"),
    ],
)
def test_the_date_room_counts_every_border_a_browser_draws(
    rendered: dict[str, str], added: str, room: float
) -> None:
    page = rendered["parent, family"]
    sheet = read_sheet(f"{stylesheet()}\n@media (max-width: 30rem) {{ {added} }}\n")
    for browser, root in ((32, 32), (16, 32)):
        assert min(date_room(sheet, page, View(320, browser, root))) == pytest.approx(room)


@pytest.mark.parametrize(
    "added",
    [
        "#add-assignments > .panel { border: calc(5vw + 5vw) solid; }",
        "#add-assignments > .panel { border: 2ch solid; }",
        "#add-assignments > .panel { border: 1px solid; border-width: initial; }",
        "#add-assignments input { border: revert; }",
        "#add-assignments input { width: 4rem; }",
        "#add-assignments .field { inline-size: 4rem; }",
        "#add-assignments .entry { min-width: 30rem; }",
        "#add-assignments .field { display: grid; }",
        "#add-assignments > .panel { display: flex; }",
        "#add-assignments > .panel { column-count: 2; }",
        "#add-assignments .field { flex-direction: row; }",
        "#add-assignments input { letter-spacing: 0.3em; }",
        "#add-assignments .field { word-spacing: 1em; }",
        "#add-assignments { zoom: 1.5; }",
    ],
)
def test_the_date_room_refuses_what_its_walk_does_not_read(
    rendered: dict[str, str], added: str
) -> None:
    page = rendered["parent, family"]
    with pytest.raises(UnreadCss):
        date_room(read_sheet(f"{stylesheet()}\n{added}\n"), page, View(320, 32, 32))


@pytest.mark.parametrize(
    ("added", "row_holds"),
    [
        pytest.param(".places { flex-shrink: 0.0; }", False, id="shrink-0.0"),
        pytest.param(".places { flex-shrink: +0; }", False, id="shrink-plus-0"),
        pytest.param(".places { flex: 0 0.0 auto; }", False, id="flex-0.0"),
        pytest.param(".places { flex: 0 .0 0px; }", False, id="flex-.0"),
        pytest.param(
            ".masthead { flex-shrink: 0; } .places { flex: inherit; }", False, id="inherit"
        ),
        pytest.param(".places { min-width: 0rem; }", True, id="least-0rem"),
        pytest.param(".places { min-width: 0%; }", True, id="least-0%"),
        pytest.param(".places { min-width: -0; }", True, id="least-minus-0"),
        pytest.param(".places { display: inline-flex; }", True, id="inline-flex"),
        pytest.param(".places { margin-left: 4rem; }", False, id="margin"),
    ],
)
def test_the_links_check_reads_each_spelling_in_a_row(
    rendered: dict[str, str], added: str, row_holds: bool
) -> None:
    sheet = read_sheet(f"{stylesheet()}\n{added}\n")
    page = rendered["parent, family"]
    for view in (View(820), View(1180, 32, 32), View(1440)):
        assert links_wrap(sheet, page, view) == row_holds, view


MASTHEAD_WRAP = """.masthead {
  display: flex;
  flex-wrap: wrap;"""


@pytest.mark.parametrize(
    "becomes",
    [
        pytest.param(MASTHEAD_WRAP.replace("\n  flex-wrap: wrap;", ""), id="no-wrap"),
        pytest.param(MASTHEAD_WRAP.replace("wrap;", "nowrap;"), id="nowrap"),
        pytest.param(MASTHEAD_WRAP.replace("wrap;", "wrap-reverse;"), id="wrap-reverse"),
        pytest.param(MASTHEAD_WRAP.replace(".masthead {", ".mastheads {"), id="selector-broken"),
    ],
)
def test_the_links_check_fails_where_a_row_that_cannot_wrap_is_too_short(
    rendered: dict[str, str], becomes: str
) -> None:
    assert becomes != MASTHEAD_WRAP
    sheet = broken(MASTHEAD_WRAP, becomes)
    for page in with_nav(rendered).values():
        head = masthead_of(page)[0]
        assert not links_wrap(sheet, page, View(481, 16, 32))
        for view in ROW_VIEWS:
            fits = column(sheet, head, view) or row_fits(sheet, head, view)
            assert links_wrap(sheet, page, view) == fits, view


@pytest.mark.parametrize(
    ("view", "line"),
    [(View(1440), 283.36), (View(1440, 16, 32), 518.72), (View(1440, 24, 24), 401.04)],
    ids=str,
)
def test_a_row_that_cannot_wrap_holds_from_exactly_its_narrowest_width(
    rendered: dict[str, str], view: View, line: float
) -> None:
    page = rendered["parent, family"]
    sides = 2 * 1.25 * view.root_text
    beside = (view.width - sides - line) / 2
    for margin, holds in ((beside, True), (beside + 0.01, False)):
        css = f"{stylesheet()}\n.masthead {{ flex-wrap: nowrap; margin: 0 {margin}px; }}\n"
        assert links_wrap(read_sheet(css), page, view) == holds, margin
    room = min(view.width, 46 * view.root_text) - sides - line
    for border, holds in ((math.floor(room) + 0.99, True), (math.floor(room) + 1, False)):
        css = f"{stylesheet()}\n.masthead {{ flex-wrap: nowrap; border-left: {border}px solid; }}\n"
        assert links_wrap(read_sheet(css), page, view) == holds, border
    for most, holds in ((line + sides, True), (line + sides - 0.01, False)):
        sheet = read_sheet(
            f"{stylesheet()}\n.masthead {{ flex-wrap: nowrap; max-width: {most}px; }}\n"
        )
        assert links_wrap(sheet, page, view) == holds, most
        wrapping = read_sheet(f"{stylesheet()}\n.masthead {{ max-width: {most}px; }}\n")
        assert links_wrap(wrapping, page, view)


@pytest.mark.parametrize(("text", "least"), [(16, 283.34), (24, 401.0), (32, 518.69)])
def test_the_row_model_leaves_what_edge_lays_out_at_each_text_size(text: int, least: float) -> None:
    """Edge's narrowest masthead row on a parent's page: brand, gap and links."""
    assert least <= ROW_PX + ROW_EM * text <= least + 0.5


def test_the_links_check_reads_a_masthead_stacked_from_the_bottom(
    rendered: dict[str, str],
) -> None:
    page = rendered["parent, family"]
    reversed_column = "@media (max-width: 30rem) { .masthead { flex-direction: column-reverse; } }"
    stacked = read_sheet(f"{stylesheet()}\n{reversed_column}\n")
    held = read_sheet(f"{stylesheet()}\n{reversed_column}\n.places {{ width: max-content; }}\n")
    for view in (View(320), View(320, 32, 32), View(320, 16, 32)):
        assert links_wrap(stacked, page, view)
        assert not links_wrap(held, page, view)
        assert brand_wraps_where_it_should(stacked, page, view)


def test_the_brand_check_fails_on_a_wide_screen_where_the_brand_is_not_a_flex_line(
    rendered: dict[str, str],
) -> None:
    sheet = broken(BRAND_BASE, BRAND_BASE.replace("inline-flex", "block"))
    for page in with_nav(rendered).values():
        assert not brand_wraps_where_it_should(sheet, page, View(1440))


def test_the_resolver_reads_attribute_names_in_any_case_and_a_comment_as_a_space() -> None:
    para, date_field, link = elements_of('<p><input type="date"><a href="/x">x</a></p>')
    for written in ("input[TYPE=date]", "input[Type='date']", "[TYPE]"):
        assert selector(written).matches(date_field), written
    assert selector("A[HREF]:link").matches(link)
    assert value_of(read_sheet("p { wid/**/th: 5px; }"), para, "width", View(320)) is None


@pytest.mark.parametrize(
    ("becomes", "fits"),
    [
        pytest.param("", False, id="no-rule"),
        pytest.param(
            DATES_RULE.replace("    padding-inline: 0;\n", "", 1), False, id="card-insets"
        ),
        pytest.param(DATES_RULE.replace("    border: 0;\n", ""), True, id="card-border"),
        pytest.param(DATES_RULE.replace("min(0.85rem, 2vw)", "0.85rem"), False, id="field-padding"),
        pytest.param(DATES_RULE.replace("2vw", "3vw"), False, id="padding-just-over"),
        pytest.param(DATES_RULE.replace("2vw", "2.9vw"), True, id="padding-just-under"),
        pytest.param(
            DATES_RULE.replace("    margin-inline: min(0px, 4vw - 1.25rem);\n", ""),
            False,
            id="page-margin-kept",
        ),
        pytest.param(DATES_RULE.replace("4vw", "5vw"), False, id="margin-just-over"),
        pytest.param(DATES_RULE.replace("4vw", "4.9vw"), True, id="margin-just-under"),
        pytest.param(
            DATES_RULE.replace("4vw - 1.25rem", "4vw - 1rem"), False, id="margin-short-of-the-page"
        ),
        pytest.param(
            DATES_RULE.replace("#add-assignments input,\n", ""), False, id="inputs-left-out"
        ),
        pytest.param(DATES_RULE.replace("> .panel", "> .card"), False, id="selector-broken"),
        pytest.param(DATES_RULE, True, id="as-it-is"),
    ],
)
def test_the_date_check_fails_where_a_date_field_is_left_too_narrow(
    rendered: dict[str, str], becomes: str, fits: bool
) -> None:
    sheet = broken(DATES_RULE, becomes)
    for page in family(rendered).values():
        for browser, root in ((32, 32), (16, 32)):
            assert (min(date_room(sheet, page, View(320, browser, root))) >= 0) == fits
        assert min(date_room(sheet, page, View(320))) >= 0


CARD_INSETS_RULE = """#add-assignments > .panel {
  padding-inline: min(1.6rem, 6vw);
}"""


@pytest.mark.parametrize(
    ("becomes", "fits"),
    [
        pytest.param("", False, id="no-rule"),
        pytest.param(CARD_INSETS_RULE.replace("6vw", "7.2vw"), False, id="share-just-over"),
        pytest.param(CARD_INSETS_RULE.replace("6vw", "7.1vw"), True, id="share-just-under"),
        pytest.param(
            CARD_INSETS_RULE.replace(" > .panel", " .panels"), False, id="selector-broken"
        ),
        pytest.param(CARD_INSETS_RULE, True, id="as-it-is"),
    ],
)
def test_the_date_check_fails_where_the_card_keeps_its_insets_just_above_the_narrow_layout(
    rendered: dict[str, str], becomes: str, fits: bool
) -> None:
    sheet = broken(CARD_INSETS_RULE, becomes)
    for page in family(rendered).values():
        assert (min(date_room(sheet, page, View(481, 16, 32))) >= 0) == fits
        assert min(date_room(sheet, page, View(481))) >= 0
        for width in (481, 640, 768, 1440):
            without = date_room(broken(CARD_INSETS_RULE, ""), page, View(width))
            assert date_room(sheet, page, View(width)) == without, width


def test_the_date_room_comes_from_every_inset_on_the_way_down() -> None:
    page = (
        '<main><details id="add-assignments"><section class="panel">'
        '<form action="/parent/inbox/read"></form><form class="entry" action="/parent/inbox/enter">'
        '<div class="field"><input type="date" id="entry-assigned_on"></div>'
        '<div class="field"><input type="date" id="entry-due_date"></div></form>'
        "</section></details></main>"
    )
    base = (
        "* { box-sizing: border-box; } main { max-width: 20rem; padding: 0 10px; }"
        " .entry { display: grid;"
        " grid-template-columns: repeat(auto-fit, minmax(min(13rem, 100%), 1fr)); }"
        " .field { display: flex; flex-direction: column; }"
        " input { border: 1px solid; padding: 0 0.5rem; }"
    )
    wide, narrow = View(1000), View(200)
    need = (7.027 + 1.2) * 16 + 10.08
    assert date_room(read_sheet(base), page, wide) == pytest.approx([208 - 18 - need] * 2)
    assert date_room(read_sheet(base), page, narrow) == pytest.approx([180 - 18 - need] * 2)
    insets = base + " section { margin: 0 3px; border-left: 2px solid; padding-right: 1em; }"
    assert date_room(read_sheet(insets), page, narrow) == pytest.approx(
        [180 - 6 - 2 - 16 - 18 - need] * 2
    )
    held = read_sheet(base + " main { max-width: 10rem; }")
    assert date_room(held, page, wide) == pytest.approx([160 - 20 - 18 - need] * 2)
    reaching = base + " section { margin-inline: min(0px, 4vw - 1.25rem); }"
    assert date_room(read_sheet(reaching), page, narrow) == pytest.approx(
        [180 + 2 * (20 - 8) - 18 - need] * 2
    )
    for unread in (
        "calc(1px)",
        "1rem + 2px",
        "min(0, 4vw - 1.25rem)",
        "min(auto, 1rem)",
        "min(0px, 4vw - 1.25rem + 0)",
        "min(0px, 4vw-1.25rem)",
    ):
        with pytest.raises(UnreadCss):
            date_room(read_sheet(base + f" section {{ margin-left: {unread}; }}"), page, narrow)
    for spelled, px in (
        ("min(1rem + 2px)", 18),
        ("min(1rem - 2px)", 14),
        ("min(10vw - 1rem + 2px)", 6),
        ("min(0px, 4vw - 1.25rem)", -12),
        ("min(0px, 20vw - 1.25rem)", 0),
    ):
        assert inset_px(spelled, narrow) == pytest.approx(px), spelled
    below = base + " input { padding: 0 min(0.5rem, 4vw - 2rem); }"
    flush = base + " input { padding: 0; }"
    assert date_room(read_sheet(below), page, narrow) == date_room(read_sheet(flush), page, narrow)
    exact = read_sheet(base + " main { max-width: 179.712px; }")
    assert date_room(exact, page, wide) == [0, 0]
    short = read_sheet(base + " main { max-width: 179.702px; }")
    assert max(date_room(short, page, wide)) < 0
    for unread in (" input { font-size: 0.9rem; }", " form { box-sizing: content-box; }"):
        with pytest.raises(UnreadCss):
            date_room(read_sheet(base + unread), page, wide)


@pytest.mark.parametrize(
    ("was", "becomes", "wrong_on"),
    [
        pytest.param(BRAND_RULE, "", "column", id="never-wraps"),
        pytest.param(BRAND_RULE, BRAND_RULE.replace("wrap;", "nowrap;"), "column", id="nowrap"),
        pytest.param(
            BRAND_RULE, BRAND_RULE.replace(".brand", ".brands"), "column", id="selector-broken"
        ),
        pytest.param(
            "  gap: 0.65rem;\n  text-decoration: none;\n",
            "  gap: 0.65rem;\n  text-decoration: none;\n  flex-wrap: wrap;\n",
            "row",
            id="wraps-in-a-row",
        ),
    ],
)
def test_the_brand_check_fails_when_the_rule_wraps_in_the_wrong_place(
    rendered: dict[str, str], was: str, becomes: str, wrong_on: str
) -> None:
    """Without the rule, the brand runs past a phone's edge; with it outside the narrow
    layout, a laptop's brand breaks onto two lines when the links squeeze it."""
    sheet = broken(was, becomes)
    for page in rendered.values():
        head = masthead_of(page)[0]
        for view in VIEWS:
            direction = value_of(sheet, head, "flex-direction", view) or "row"
            expected = direction != wrong_on
            assert brand_wraps_where_it_should(sheet, page, view) == expected, view


NARROW = "@media (max-width: 30rem) { %s }"


@pytest.mark.parametrize(
    "added",
    [
        pytest.param(NARROW % ".brand { width: max-content; }", id="max-content"),
        pytest.param(NARROW % ".brand { inline-size: max-content; }", id="inline-size"),
        pytest.param(NARROW % ".brand { min-width: max-content; }", id="min-width-max-content"),
        pytest.param(NARROW % ".brand { width: 30rem; }", id="fixed-width"),
        pytest.param(NARROW % ".brand { min-inline-size: 20rem; }", id="fixed-min-width"),
        pytest.param(NARROW % ".brand { width: 120%; }", id="past-the-line"),
        pytest.param(NARROW % ".brand { display: block; }", id="not-a-flex-line"),
        pytest.param(NARROW % ".brand { flex-flow: row nowrap; }", id="flex-flow-nowrap"),
        pytest.param(NARROW % ".brand { flex-direction: column; }", id="stacked"),
        pytest.param(NARROW % ".brand { flex-wrap: wrap-reverse; }", id="wrap-reverse"),
        pytest.param(NARROW % ".brand { width: 99.99%; }", id="just-short-of-the-line"),
        pytest.param(NARROW % ".brand { min-width: 100px; }", id="past-a-narrow-line"),
        pytest.param(NARROW % ".brand { WIDTH: MAX-CONTENT; }", id="capitals"),
        pytest.param(
            NARROW % ".brand { width: max-content !important; } .brand { width: 100%; }",
            id="important",
        ),
        pytest.param(
            NARROW % "a.brand:link { width: max-content; } .masthead .brand { width: 100%; }",
            id="pseudo-class-weight",
        ),
    ],
)
def test_the_brand_check_fails_when_the_brand_can_run_past_the_line(
    rendered: dict[str, str], added: str
) -> None:
    """A brand as wide as its content on one line, or given a width of its own past the
    line, runs past a phone's edge with large text even though its flex line wraps."""
    sheet = read_sheet(stylesheet() + "\n" + added + "\n")
    for page in rendered.values():
        head = masthead_of(page)[0]
        for view in VIEWS:
            column = value_of(sheet, head, "flex-direction", view) == "column"
            assert brand_wraps_where_it_should(sheet, page, view) is not column, view


@pytest.mark.parametrize(
    "added", [".brand { flex-direction: column; }", ".brand { flex-wrap: wrap-reverse; }"]
)
def test_the_brand_check_fails_on_every_screen_where_the_brand_stacks_or_wraps_upward(
    rendered: dict[str, str], added: str
) -> None:
    sheet = read_sheet(stylesheet() + "\n" + added + "\n")
    for page in rendered.values():
        for view in VIEWS:
            assert not brand_wraps_where_it_should(sheet, page, view), view


def test_the_brand_takes_two_lines_one_pixel_past_a_fit() -> None:
    """The mark and the wordmark share a line they fill exactly and take two once they are a
    pixel wider; a wordmark wider than the line takes the brand with it and no further."""
    content = Sizing(True, "auto", "auto", "none", False, 16)
    assert laid_out(content, 240, 48, 20, 172) == (240, 1)
    assert laid_out(content, 240, 48, 20, 173) == (240, 2)
    assert laid_out(content, 240, 48, 20, 100) == (168, 1)
    assert laid_out(content, 240, 48, 20, 241) == (241, 2)
    assert laid_out(replace(content, stretched=True), 240, 48, 20, 100) == (240, 1)
    assert laid_out(replace(content, wraps=False), 240, 48, 20, 173) == (241, 1)
    assert laid_out(replace(content, width="100%", least="0"), 240, 48, 20, 173) == (240, 2)
    assert laid_out(replace(content, most="10rem"), 240, 48, 20, 172) == (160, 2)
    assert laid_out(replace(content, least="20rem"), 240, 48, 20, 100) == (320, 1)
    assert laid_out(replace(content, width="min-content"), 240, 48, 20, 100) == (100, 2)
    assert laid_out(replace(content, width="fit-content"), 240, 48, 20, 173) == (240, 2)
    assert laid_out(replace(content, least="20rem", most="10rem"), 240, 48, 20, 100) == (320, 1)
    assert laid_out(replace(content, most="10rem", root_text=32), 240, 48, 20, 172) == (240, 1)
    with pytest.raises(UnreadCss):
        laid_out(replace(content, width="calc(100% - 1rem)"), 240, 48, 20, 100)


def test_the_brand_sweep_takes_each_mark_and_wordmark_that_fits_the_line(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Up to the line's width exactly; a wider one could stay within it only by breaking."""
    taken: list[tuple[float, float, float]] = []
    real = laid_out

    def recorded(
        sizing: Sizing, line: float, mark: float, gap: float, word: float
    ) -> tuple[float, int]:
        taken.append((line, mark, word))
        return real(sizing, line, mark, gap, word)

    monkeypatch.setitem(globals(), "laid_out", recorded)
    assert keeps_within_the_line.__wrapped__(Sizing(True, "auto", "auto", "none", False, 16))
    assert all(max(mark, word) <= line for line, mark, word in taken)
    assert any(word == line for line, _, word in taken)
    assert (160, 160.0) in {(line, mark) for line, mark, _ in taken}
    assert {line for line, _, _ in taken} == set(LINES)


@pytest.mark.parametrize(("past", "holds"), [(0.0, True), (0.01, False), (1.0, False)])
def test_the_brand_check_fails_where_the_brand_is_wider_than_its_line(
    monkeypatch: pytest.MonkeyPatch, past: float, holds: bool
) -> None:
    def wider(
        sizing: Sizing, line: float, mark: float, gap: float, word: float
    ) -> tuple[float, int]:
        return line + past, 2 if mark + gap + word > line else 1

    monkeypatch.setitem(globals(), "laid_out", wider)
    sizing = Sizing(True, "auto", "auto", "none", True, 16)
    assert keeps_within_the_line.__wrapped__(sizing) is holds


@pytest.mark.parametrize(
    ("css", "expected"),
    [
        pytest.param("", Sizing(False, "auto", "auto", "none", True, 16), id="nothing-set"),
        pytest.param(
            ".masthead { align-items: flex-start; } .brand { flex-wrap: wrap; }",
            Sizing(True, "auto", "auto", "none", False, 16),
            id="content-wide",
        ),
        pytest.param(
            ".masthead { align-items: flex-start; } .brand { align-self: stretch; }",
            Sizing(False, "auto", "auto", "none", True, 16),
            id="stretched-alone",
        ),
        pytest.param(
            ".masthead { place-items: stretch; } .brand { place-self: center stretch; }",
            Sizing(False, "auto", "auto", "none", False, 16),
            id="centered-alone",
        ),
        pytest.param(
            ".brand { inline-size: 50%; min-width: 1px; max-inline-size: 90%; }",
            Sizing(False, "50%", "1px", "90%", True, 16),
            id="logical-widths",
        ),
    ],
)
def test_the_brand_sizing_comes_from_the_cascade(css: str, expected: Sizing) -> None:
    """With nothing set, a flex item is stretched across a column; its own alignment wins
    over the masthead's."""
    page = elements_of('<header class="masthead"><a class="brand"><img><span>b</span></a></header>')
    head, brand = page[0], page[1]
    assert sizing_of(read_sheet(css), head, brand, View(320)) == expected
    with pytest.raises(UnreadCss):
        sizing_of(read_sheet(".brand { align-self: safe center; }"), head, brand, View(320))


@pytest.mark.parametrize(
    "added",
    [
        pytest.param(NARROW % ".brand { width: 100%; min-width: 0; }", id="the-line-wide"),
        pytest.param(NARROW % ".masthead { align-items: stretch; }", id="stretched"),
        pytest.param(NARROW % ".brand { place-self: stretch; }", id="stretched-alone"),
        pytest.param(
            NARROW % ".brand { width: fit-content; max-inline-size: 100%; }", id="fit-content"
        ),
        pytest.param(".brand { min-width: auto; max-width: none; }", id="no-limits"),
        pytest.param(NARROW % ".brand { Width: 100% !important; min-width: 0; }", id="spelled"),
    ],
)
def test_the_brand_check_holds_for_a_brand_kept_within_the_line(
    rendered: dict[str, str], added: str
) -> None:
    """Stretched across the masthead or as wide as its content, a brand that wraps stays
    within the line."""
    sheet = read_sheet(stylesheet() + "\n" + added + "\n")
    for page in rendered.values():
        for view in VIEWS:
            assert brand_wraps_where_it_should(sheet, page, view), view


@pytest.mark.parametrize(
    "becomes",
    [
        pytest.param("grid-template-columns: repeat(auto-fit, minmax(13rem, 1fr));", id="13rem"),
        pytest.param(ENTRY_COLUMNS.replace("100%", "13rem"), id="13rem-twice"),
        pytest.param(ENTRY_COLUMNS.replace("min(13rem", "min(10rem"), id="10rem"),
        pytest.param(ENTRY_COLUMNS.replace("100%", "90%"), id="90-percent"),
        pytest.param(
            ENTRY_COLUMNS.replace("grid-template-columns", "grid-template-rows"), id="no-columns"
        ),
    ],
)
def test_the_columns_check_fails_when_a_column_can_outgrow_the_form_or_changes_width(
    rendered: dict[str, str], becomes: str
) -> None:
    """A floor of 13rem alone, or of 13rem twice, is wider than a phone's form at 200%; a
    floor of 10rem or of 90% changes the columns on a wide screen; and with no columns
    given, the form is one column everywhere."""
    sheet = broken(ENTRY_COLUMNS, becomes)
    for page in family(rendered).values():
        assert not any(entry_columns_fit(sheet, page, view) for view in VIEWS)


# The paste form is laid out as a form that asks for a decision, so the rule for those forms
# breaks a word in its button too: with the Add assignments rule gone or narrowed, that button
# still breaks its word.
DECIDED = "Preview assignments"


@pytest.mark.parametrize(
    ("becomes", "missed"),
    [
        pytest.param("", [name for _, name in ADDING_CONTROLS if name != DECIDED], id="no-rule"),
        pytest.param(
            ADDING_RULE.replace("anywhere", "break-word"),
            [name for _, name in ADDING_CONTROLS],
            id="break-word",
        ),
        pytest.param(
            ADDING_RULE.replace("#add-assignments", ".entry"),
            ["Add assignments", "School text"],
            id="entry-form-only",
        ),
        pytest.param(
            ADDING_RULE.replace("#add-assignments > summary,\n", ""),
            ["Add assignments"],
            id="summary-left-out",
        ),
        pytest.param(
            ADDING_RULE.replace("#add-assignments > summary", "#add-assignments .steps summary"),
            ["Add assignments"],
            id="the-example-summary-only",
        ),
        pytest.param(
            ADDING_RULE + "\n#add-assignments > summary { word-wrap: normal; }",
            ["Add assignments"],
            id="undone-by-word-wrap",
        ),
        pytest.param(
            ADDING_RULE.replace("overflow-wrap", "word-wrap"), [], id="written-as-word-wrap"
        ),
        pytest.param(
            ADDING_RULE.replace("#add-assignments label,\n", ""),
            [name for tag, name in ADDING_CONTROLS if tag == "label"],
            id="buttons-only",
        ),
        pytest.param(
            ADDING_RULE.replace(",\n#add-assignments button", ""),
            [name for tag, name in ADDING_CONTROLS if tag == "button" and name != DECIDED],
            id="labels-only",
        ),
    ],
)
def test_the_words_check_fails_for_each_control_the_rule_stops_reaching(
    rendered: dict[str, str], becomes: str, missed: list[str]
) -> None:
    sheet = broken(ADDING_RULE, becomes)
    for page in family(rendered).values():
        for view in VIEWS:
            assert words_break_where_they_must(sheet, page, view) == missed, view


def test_the_words_check_fails_where_a_rule_holds_only_while_the_fold_is_open(
    rendered: dict[str, str], opened: dict[str, str]
) -> None:
    sheet = read_sheet(
        stylesheet() + "\n#add-assignments[open] > summary { overflow-wrap: normal; }\n"
    )
    assert len(opened) == 4
    for view in VIEWS:
        for page in family(rendered).values():
            assert words_break_where_they_must(sheet, page, view) == [], view
        for page in opened.values():
            assert words_break_where_they_must(sheet, page, view) == ["Add assignments"], view


FORMS = '<form action="/parent/inbox/read"></form><form action="/parent/inbox/enter"></form>'


@pytest.mark.parametrize(
    "page",
    [
        pytest.param(
            f'<main><div id="add-assignments"><summary>Add</summary>{FORMS}</div></main>',
            id="not-a-disclosure",
        ),
        pytest.param(
            '<main><details id="add-assignments"><summary>Add</summary><summary>More</summary>'
            f"{FORMS}</details></main>",
            id="two-summaries",
        ),
        pytest.param(
            '<main><details id="add-assignments"><section><summary>Add</summary></section>'
            f"{FORMS}</details></main>",
            id="summary-further-down",
        ),
        pytest.param(f"<main><summary>Add</summary>{FORMS}</main>", id="no-fold"),
    ],
)
def test_the_entry_surface_is_read_only_from_the_fold_and_its_own_summary(page: str) -> None:
    with pytest.raises(AssertionError):
        entry_surface_of(page)
    whole = f'<main><details id="add-assignments"><summary>Add</summary>{FORMS}</details></main>'
    assert entry_surface_of(whole)[0].text == "Add"


@pytest.mark.parametrize(
    "written",
    [
        "#add-assignments > > summary",
        "#add-assignments >>> summary",
        "#add-assignments > summary >",
        "> summary",
        "",
        "  ",
    ],
)
def test_the_resolver_refuses_a_selector_a_browser_drops(written: str) -> None:
    with pytest.raises(UnreadCss):
        selector(written)
    with pytest.raises(UnreadCss):
        read_sheet(f"{written}, button {{ overflow-wrap: anywhere; }}")


def test_the_resolver_refuses_a_reset_of_every_property() -> None:
    with pytest.raises(UnreadCss):
        read_sheet(stylesheet() + "\n#add-assignments > summary { all: unset; }\n")


@pytest.mark.parametrize(
    "added",
    [
        ".places a { white-space: nowrap; }",
        "@media (max-width: 30rem) { .wordmark { font-size: 1.2rem; } }",
        ".masthead { overflow-x: hidden; }",
        ".entry label { text-overflow: ellipsis; }",
        "@media (max-width: 30rem) { #add-assignments button { font-size: 0.8rem; } }",
        "#add-assignments > summary { white-space: nowrap; }",
        "@media (max-width: 30rem) { .panel-fold > summary { font-size: 1rem; } }",
        "#add-assignments > summary { text-wrap: nowrap; }",
        "#add-assignments { text-wrap: nowrap; }",
        "@media (max-width: 30rem) { #add-assignments > summary { zoom: 0.8; } }",
        "#add-assignments > summary { text-wrap-mode: nowrap; }",
        "#add-assignments[open] > summary { white-space: nowrap; }",
        "body { overflow-x: hidden; }",
        "main { overflow: clip; }",
        "#add-assignments > .panel { overflow: hidden; }",
    ],
)
def test_the_cut_check_fails_when_a_rule_holds_cuts_or_shrinks_a_line(
    rendered: dict[str, str], opened: dict[str, str], added: str
) -> None:
    sheet = read_sheet(stylesheet() + "\n" + added + "\n")
    found = [
        problem
        for name, page in (rendered | opened).items()
        for problem in cut_or_shrunk(
            sheet, masthead_of(page) + (entry_surface_of(page) if name.endswith("family") else [])
        )
    ]
    assert found


# ------------------------------------------------------------- the text of every page


ASK = "/student/actions/ask-for-help"
UNREAD = {
    "her": f"/student/actions/assignments/{ESSAY_ID}/report",
    "parent": f"/parent/actions/checks/{ESSAY_ID}/mark",
    "open": f"/student/actions/assignments/{ESSAY_ID}/report",
}
"""A form each reader can send, to be sent as multipart its parser can't read."""
UNREADABLE = b"not the boundary\r\n\r\nx"
GONE = "/student/assignments/assignment-not-on-record"
HEADINGS = frozenset({"h1", "h2", "h3"})

TEXT: dict[str, str] = {}


@pytest.fixture
def text_pages(tmp_path: pathlib.Path) -> dict[str, str]:
    """Once she has asked for help, for her, a signed-in parent, and with the sign-in off:
    the homework notes, an assignment that is not on record, and a form that couldn't be
    read; and, for a parent and with the sign-in off, the family page with the reply form for
    her request. Rendered by the first test that asks and kept for the rest."""
    if TEXT:
        return TEXT
    for reader in ("her", "parent", "open"):
        folder = tmp_path / "text" / reader
        folder.mkdir(parents=True)
        with household_client(reader, folder) as client:
            sign_in_as(client, "open" if reader == "open" else "her")
            week = client.get(HER_PAGE, headers=PAGE_HEADERS).text
            asked = form_fields(week, ASK) | {"note": "Which part is due first?"}
            assert client.post(ASK, data=asked, headers=PAGE_HEADERS).status_code == 303
            sign_in_as(client, reader)
            answer = client.post(
                UNREAD[reader],
                content=UNREADABLE,
                headers={**PAGE_HEADERS, "Content-Type": "multipart/form-data; boundary=x"},
            )
            assert answer.status_code == 400
            TEXT[f"{reader}, unread form"] = answer.text
            notes = client.get("/student/homework-notes", headers=PAGE_HEADERS)
            TEXT[f"{reader}, notes"] = notes.text
            TEXT[f"{reader}, gone"] = client.get(GONE, headers=PAGE_HEADERS).text
            if reader != "her":
                page = client.get("/parent", headers=PAGE_HEADERS).text
                assert 'action="/parent/actions/help/' in page
                TEXT[f"{reader}, asked for help, family"] = page
    return TEXT


def headings_of(page: str) -> list[Element]:
    return [one for one in elements_of(page) if one.tag in HEADINGS]


def fold_notes_of(page: str) -> list[Element]:
    """Each note inside one of the family page's folds, Help with a plan's among them."""
    found = elements_of(page)
    folds = [one for one in found if one.tag == "details" and "panel-fold" in one.classes]
    notes = [
        one
        for one in found
        if one.tag == "p" and "note" in one.classes and any(fold_holds(f, one) for f in folds)
    ]
    assert any("ANTHROPIC_API_KEY" in one.text for one in notes)
    return notes


def example_of(page: str) -> Element:
    """The example of school text in Add assignments."""
    (example,) = [one for one in elements_of(page) if one.tag == "pre"]
    assert "body" in example.classes
    return example


def whole_words(sheet: Sheet, held: list[Element], view: View) -> list[str]:
    """Each of ``held`` that keeps a word wider than its line whole on ``view``, so the word
    runs past the line. `anywhere` breaks it, and lets the element be narrower than the word
    wherever its width comes from what it holds."""
    return [
        f"{one.tag}.{'.'.join(sorted(one.classes))} {' '.join(one.text.split())[:40]}"
        for one in held
        if value_of(sheet, one, "overflow-wrap", view) != "anywhere"
    ]


def decisions_widened(sheet: Sheet, page: str, view: View) -> list[str]:
    """Each form on the page that asks the family for a decision, a column of controls as
    wide as the form, where something can make it wider on ``view``: a column that wraps,
    each of whose lines is as wide as its widest control, so a field's own width widens it;
    controls that do not stretch to the form's width; or a button that keeps a word wider
    than its line whole."""
    found = elements_of(page)
    forms = [one for one in found if one.tag == "form" and "decision" in one.classes]
    assert forms
    widened = []
    for form in forms:
        laid = {
            name: value_of(sheet, form, name, view)
            for name in ("display", "flex-direction", "flex-wrap", "align-items")
        }
        if (
            laid["display"] != "flex"
            or laid["flex-direction"] != "column"
            or laid["flex-wrap"] not in (None, "nowrap")
            or laid["align-items"] not in (None, "normal", "stretch")
        ):
            widened.append(f"{form.attributes['action']} {laid}")
        buttons = [one for one in found if one.tag == "button" and one.within(form)]
        widened += [
            f"{form.attributes['action']} {one}" for one in whole_words(sheet, buttons, view)
        ]
    return widened


FOCUSED = re.compile(r"(?<!:):(focus(?:-visible|-within)?)(?![\w-])", re.IGNORECASE)
"""A focus pseudo-class, in any case, as a browser reads it. After `::` it names a
pseudo-element, which never matches."""
HELD = {state: f"{state}-held" for state in ("focus", "focus-visible", "focus-within")}
"""The attribute that stands in for each focus pseudo-class while an element has focus."""


def holding_focus(element: Element, *, within: bool = False) -> Element:
    """A copy of ``element`` and the elements above it as they are while it has the
    keyboard's focus: each copy above it carries the `:focus-within` stand-in, and the copy
    of ``element`` all three. A page that already carries a stand-in is refused."""
    if not element.attributes.keys().isdisjoint(HELD.values()):
        raise UnreadCss(element.tag)
    above = None if element.parent is None else holding_focus(element.parent, within=True)
    held = [HELD["focus-within"]] if within else list(HELD.values())
    return replace(element, attributes=element.attributes | dict.fromkeys(held, ""), parent=above)


def focused_selector(head: str) -> Selector:
    """``head``, as `style_rules` gives it, with each focus pseudo-class read as its stand-in
    attribute, which weighs as much. A selector that names a stand-in is refused."""
    if not {one.lower() for one in re.findall(r"\[([\w-]+)", head)}.isdisjoint(HELD.values()):
        raise UnreadCss(head)
    return selector(unshielded(FOCUSED.sub(lambda state: f"[{HELD[state[1].lower()]}]", head)))


def focused_value(css: str, element: Element, name: str, view: View) -> str | None:
    """The value the cascade gives ``name`` on ``element`` while it has the keyboard's
    focus: `:focus` and `:focus-visible` hold on ``element`` alone, `:focus-within` on it and
    every element above it, and each keeps the weight of a pseudo-class."""
    focused = holding_focus(element)
    rules = [
        Rule(focused_selector(head), value.lower(), style.order, style.media, important)
        for style in style_rules(css)
        for head in style.selectors.split(",")
        for named, value, important in style.declarations
        if named == name
    ]
    found = winner([rule for rule in rules if rule.chosen.matches(focused)], view)
    return None if found is None else found.value


def extent_px(value: str, view: View) -> float:
    """A width measured against the screen: a length, a percentage of the screen's width, or
    a ``calc()`` of them added and taken away."""
    inner = re.fullmatch(r"calc\((.+)\)", value)
    read = re.sub(
        r"(\d*\.?\d+)%",
        lambda share: f"{float(share.group(1)) * view.width / 100}px",
        inner.group(1) if inner else value,
    )
    return summed_px(read, view)


def skip_outline_room(css: str, page: str, view: View) -> tuple[float, float]:
    """How far inside the screen's left and right edges the focused skip link's outline
    stays on ``view``, in pixels, with the link at its widest; below zero, the outline runs
    past. The link is placed against the screen, so a percentage is of the screen's width,
    and a link with no widest width of its own reaches the screen's right edge."""
    (skip,) = [one for one in elements_of(page) if one.tag == "a" and "skip" in one.classes]
    left = length_px(focused_value(css, skip, "left", view) or "", view)
    most = focused_value(css, skip, "max-width", view)
    widest = view.width - left
    if most is not None and most != "none":
        widest = min(widest, extent_px(most, view))
    drawn = (focused_value(css, skip, "outline", view) or "none").split()
    lengths = [one for one in drawn if re.fullmatch(r"\d*\.?\d+(px|rem|em)", one)]
    reach = 0.0
    if "none" not in drawn and lengths:
        offset = focused_value(css, skip, "outline-offset", view)
        reach = length_px(lengths[0], view) + (length_px(offset, view) if offset else 0.0)
    return round(left - reach, 6), round(view.width - left - widest - reach, 6)


@pytest.mark.parametrize("view", VIEWS, ids=str)
def test_every_heading_breaks_a_word_only_when_it_cannot_fit(
    rendered: dict[str, str],
    opened: dict[str, str],
    text_pages: dict[str, str],
    view: View,
) -> None:
    """Her week's Today heading, the homework notes' heading, the headings of the family
    page, of a page saying an assignment is not on record and of one saying a form couldn't
    be read, for every reader: none keeps a word wider than its line whole."""
    sheet = read_sheet(stylesheet())
    for name, page in (rendered | opened | text_pages).items():
        assert whole_words(sheet, headings_of(page), view) == [], name


def test_no_heading_or_fold_note_is_cut_or_shrunk_on_any_screen(
    rendered: dict[str, str], opened: dict[str, str], text_pages: dict[str, str]
) -> None:
    sheet = read_sheet(stylesheet())
    for name, page in (rendered | opened | text_pages).items():
        held = headings_of(page) + (fold_notes_of(page) if name.endswith("family") else [])
        assert cut_or_shrunk(sheet, held) == [], name


@pytest.mark.parametrize("view", VIEWS, ids=str)
def test_the_family_pages_fold_notes_and_example_break_a_long_word(
    rendered: dict[str, str], opened: dict[str, str], text_pages: dict[str, str], view: View
) -> None:
    """Help with a plan's note names a setting as one long word, and the example of school
    text has a date run into a word; both break such a word and the example keeps its lines,
    with the folds closed and open."""
    sheet = read_sheet(stylesheet())
    for name, page in (family(rendered) | opened | family(text_pages)).items():
        assert whole_words(sheet, fold_notes_of(page), view) == [], name
        example = example_of(page)
        assert whole_words(sheet, [example], view) == [], name
        assert value_of(sheet, example, "white-space", view) == "pre-wrap", name


@pytest.mark.parametrize("view", VIEWS, ids=str)
def test_a_decision_form_is_one_column_as_wide_as_the_form(
    rendered: dict[str, str], opened: dict[str, str], text_pages: dict[str, str], view: View
) -> None:
    """The reply to her request for help, and the paste form beside it, for a parent and
    with the sign-in off."""
    sheet = read_sheet(stylesheet())
    for name, page in (family(rendered) | opened | family(text_pages)).items():
        assert decisions_widened(sheet, page, view) == [], name


@pytest.mark.parametrize("view", VIEWS, ids=str)
def test_the_skip_links_focus_outline_stays_on_the_screen(
    rendered: dict[str, str], text_pages: dict[str, str], view: View
) -> None:
    css = stylesheet()
    for name, page in (rendered | text_pages).items():
        assert min(skip_outline_room(css, page, view)) >= 0, name


@pytest.mark.parametrize(
    ("first", "rule", "name", "value"),
    [
        pytest.param(False, "a.skip { left: -999px; }", "left", "1rem", id="type-and-class"),
        pytest.param(True, ".skip.skip { left: 2rem; }", "left", "1rem", id="two-classes-first"),
        pytest.param(False, "a.skip.skip { left: 2rem; }", "left", "2rem", id="one-over"),
        pytest.param(False, "body:focus .skip { left: -999px; }", "left", "1rem", id="parent"),
        pytest.param(
            False, "body:focus-visible > .skip { left: -999px; }", "left", "1rem", id="visible"
        ),
        pytest.param(False, ".skip:not(:focus) { left: -999px; }", "left", "1rem", id="not"),
        pytest.param(False, "body:not(:focus) .skip { left: 2rem; }", "left", "2rem", id="not-up"),
        pytest.param(False, ".skip:FOCUS { left: 2rem; }", "left", "2rem", id="any-case"),
        pytest.param(
            False, ".skip:Focus-Visible { left: 2rem; }", "left", "2rem", id="visible-case"
        ),
        pytest.param(False, ".skip:focus-within { left: 2rem; }", "left", "2rem", id="within"),
        pytest.param(
            False, "body:focus-within .skip { left: 2rem; }", "left", "2rem", id="within-up"
        ),
        pytest.param(
            False, "html:focus-within > body > a { left: 2rem; }", "left", "1rem", id="within-root"
        ),
        pytest.param(
            False,
            "body > .skip:first-child:focus { left: 2rem; }",
            "left",
            "2rem",
            id="first-child",
        ),
        pytest.param(False, ".skip::focus { left: 2rem; }", "left", "1rem", id="pseudo-element"),
        pytest.param(
            False, '.skip:not([title=":focus"]) { left: 2rem; }', "left", "2rem", id="quoted"
        ),
        pytest.param(
            True,
            "a.skip { outline: none; }",
            "outline",
            "3px solid var(--blue-action)",
            id="outline-first",
        ),
    ],
)
def test_the_focused_skip_link_takes_the_value_a_browser_gives_it(
    rendered: dict[str, str], first: bool, rule: str, name: str, value: str
) -> None:
    """A rule added to the stylesheet, and the value Edge gives the link reached by Tab."""
    css = f"{rule}\n{stylesheet()}" if first else f"{stylesheet()}\n{rule}\n"
    for page_name, page in rendered.items():
        (skip,) = [one for one in elements_of(page) if one.tag == "a" and "skip" in one.classes]
        assert focused_value(css, skip, name, View(320)) == value, page_name


@pytest.mark.parametrize(
    ("rule", "carried"),
    [
        pytest.param(".skip:focusx { left: 2rem; }", ("", ""), id="unknown-state"),
        pytest.param(".skip[focus-held] { left: 2rem; }", ("", ""), id="named"),
        pytest.param("a[Focus-Within-Held] { left: 2rem; }", ("", ""), id="named-any-case"),
        pytest.param("", ('<a class="skip"', '<a class="skip" focus-visible-held'), id="link"),
        pytest.param("", ("<body", "<body focus-within-held"), id="above-the-link"),
    ],
)
def test_the_focused_skip_link_is_refused_where_its_stand_ins_could_be_misread(
    rendered: dict[str, str], rule: str, carried: tuple[str, str]
) -> None:
    css = f"{stylesheet()}\n{rule}\n"
    for page in rendered.values():
        shown = page.replace(*carried, 1)
        (skip,) = [one for one in elements_of(shown) if one.tag == "a" and "skip" in one.classes]
        with pytest.raises(UnreadCss):
            focused_value(css, skip, "left", View(320))


# ------------------------------------------------------------- and each of these fails when broken


HEADINGS_RULE = """h1,
h2,
h3 {
  overflow-wrap: anywhere;
}"""
FOLD_NOTES_RULE = """.panel-fold .note {
  overflow-wrap: anywhere;
}"""
EXAMPLE_RULE = """pre.body {
  white-space: pre-wrap;
  overflow-wrap: anywhere;"""
DECISION_RULE = """.decision {
  flex-direction: column;
  flex-wrap: nowrap;
  align-items: stretch;
}"""
DECISION_BUTTONS_RULE = """.decision button {
  overflow-wrap: anywhere;
}"""
SKIP_WIDTH = "  max-width: calc(100% - 2rem);\n"


@pytest.mark.parametrize(
    ("becomes", "missed"),
    [
        pytest.param("", {"h1", "h2", "h3"}, id="no-rule"),
        pytest.param(
            HEADINGS_RULE.replace("anywhere", "break-word"), {"h1", "h2", "h3"}, id="break-word"
        ),
        pytest.param(HEADINGS_RULE.replace("h2,\nh3", "h2"), {"h3"}, id="h3-left-out"),
        pytest.param(HEADINGS_RULE.replace("h1,\n", ""), {"h1"}, id="h1-left-out"),
        pytest.param(
            HEADINGS_RULE + "\n.today h2 { overflow-wrap: normal; }", {"h2"}, id="undone-for-today"
        ),
        pytest.param(HEADINGS_RULE.replace("overflow-wrap", "word-wrap"), set(), id="word-wrap"),
    ],
)
def test_the_headings_check_fails_for_each_heading_the_rule_stops_reaching(
    rendered: dict[str, str], text_pages: dict[str, str], becomes: str, missed: set[str]
) -> None:
    sheet = broken(HEADINGS_RULE, becomes)
    for view in VIEWS:
        found = {
            one.split(".")[0]
            for page in (rendered | text_pages).values()
            for one in whole_words(sheet, headings_of(page), view)
        }
        assert found == missed, view


def test_the_headings_check_fails_where_the_rule_holds_only_on_a_narrow_screen(
    rendered: dict[str, str],
) -> None:
    sheet = broken(HEADINGS_RULE, f"@media (max-width: 30rem) {{ {HEADINGS_RULE} }}")
    for view in VIEWS:
        missed = [
            one for page in rendered.values() for one in whole_words(sheet, headings_of(page), view)
        ]
        assert bool(missed) == (view.width * 16 > 480 * view.browser_text), view


@pytest.mark.parametrize(
    "added",
    [
        "h1 { white-space: nowrap; }",
        "@media (max-width: 30rem) { h2 { font-size: 1rem; } }",
        ".today h2 { text-overflow: ellipsis; }",
        ".panel-fold .note { white-space: nowrap; }",
        "section { overflow-x: hidden; }",
    ],
)
def test_the_cut_check_fails_when_a_heading_or_a_fold_note_is_cut_or_shrunk(
    rendered: dict[str, str], added: str
) -> None:
    sheet = read_sheet(stylesheet() + "\n" + added + "\n")
    found = [
        problem
        for name, page in rendered.items()
        for problem in cut_or_shrunk(
            sheet, headings_of(page) + (fold_notes_of(page) if name.endswith("family") else [])
        )
    ]
    assert found


@pytest.mark.parametrize(
    ("was", "becomes"),
    [
        pytest.param(FOLD_NOTES_RULE, "", id="fold-notes-no-rule"),
        pytest.param(
            FOLD_NOTES_RULE,
            FOLD_NOTES_RULE.replace("anywhere", "break-word"),
            id="fold-notes-break",
        ),
        pytest.param(
            FOLD_NOTES_RULE, FOLD_NOTES_RULE.replace(" .note", " > .note"), id="fold-notes-children"
        ),
        pytest.param(
            FOLD_NOTES_RULE,
            FOLD_NOTES_RULE.replace(".panel-fold", "#add-assignments"),
            id="fold-notes-one-fold",
        ),
        pytest.param(EXAMPLE_RULE, "pre.body {\n  white-space: pre-wrap;", id="example-no-break"),
        pytest.param(EXAMPLE_RULE, EXAMPLE_RULE.replace("anywhere", "normal"), id="example-normal"),
        pytest.param(EXAMPLE_RULE, EXAMPLE_RULE.replace("pre-wrap", "pre"), id="example-one-line"),
    ],
)
def test_the_fold_notes_check_fails_when_a_note_or_the_example_keeps_a_long_word(
    rendered: dict[str, str], was: str, becomes: str
) -> None:
    sheet = broken(was, becomes)
    for view in VIEWS:
        for page in family(rendered).values():
            example = example_of(page)
            assert (
                whole_words(sheet, [*fold_notes_of(page), example], view)
                or value_of(sheet, example, "white-space", view) != "pre-wrap"
            ), view


@pytest.mark.parametrize(
    ("was", "becomes"),
    [
        pytest.param(
            DECISION_RULE, DECISION_RULE.replace("  flex-wrap: nowrap;\n", ""), id="wraps"
        ),
        pytest.param(DECISION_RULE, DECISION_RULE.replace("nowrap", "wrap-reverse"), id="reverse"),
        pytest.param(DECISION_RULE, DECISION_RULE.replace("stretch", "flex-start"), id="start"),
        pytest.param(DECISION_BUTTONS_RULE, "", id="buttons-whole"),
        pytest.param(
            DECISION_BUTTONS_RULE,
            DECISION_BUTTONS_RULE.replace(" button", " > button"),
            id="buttons-children-only",
        ),
    ],
)
def test_the_decision_check_fails_when_a_form_can_widen(
    text_pages: dict[str, str], was: str, becomes: str
) -> None:
    sheet = broken(was, becomes)
    for view in VIEWS:
        for name, page in family(text_pages).items():
            assert any(
                "/parent/actions/help/" in one for one in decisions_widened(sheet, page, view)
            ), (name, view)


# ------------------------------------------------------------- side padding that gives way

GIVING_WAY = """.decision button,
.help-panel .ask button {
  padding-inline: clamp(0px, 13vw - 1rem, 1.35rem);
}

.help-panel .problem {
  padding-inline: clamp(0px, 13vw - 1rem, 0.95rem);
}"""
"""The side padding of a decision's buttons, of her Ask a parent for help in Help, and of a
problem line in Help, as the stylesheet writes it: measured in Edge at 320 pixels with text
at 200%, where it leaves each of their words whole, and on wider screens, where it changes
nothing. The sign-in button and the button that asks about one note are outside its reach."""
GIVES_WAY = {
    "button": "clamp(0px, 13vw - 1rem, 1.35rem)",
    "p": "clamp(0px, 13vw - 1rem, 0.95rem)",
}

SET_APART: dict[str, str] = {}


@pytest.fixture
def set_apart_pages(tmp_path: pathlib.Path) -> dict[str, str]:
    """Her week, for her and for a signed-in parent, with a request for help that can't be
    read set apart beside a readable one. Rendered by the first test that asks."""
    if SET_APART:
        return SET_APART
    for reader in ("her", "parent"):
        folder = tmp_path / "set-apart" / reader
        folder.mkdir(parents=True)
        with household_client(reader, folder) as client:
            store, today = state_of(client).help_requests, state_of(client).clock.today()
            store.ask(today, "Synthetic open question")
            damaged = store.ask(today, "Synthetic question").request_id
            store._connection.execute(
                "UPDATE help_requests SET note = note || printf('%.*c', 600, 'x') "
                "WHERE request_id = ?",
                (damaged,),
            )
            store._connection.commit()
            sign_in_as(client, reader)
            page = client.get(HER_PAGE, headers=PAGE_HEADERS).text
            assert "1 request for help can&#39;t be read right now." in page
            SET_APART[f"{reader}, her week"] = page
    return SET_APART


def giving_way(page: str) -> list[Element]:
    """The buttons of each decision form and of her ask form in Help, and each problem line in
    Help."""
    found = elements_of(page)
    panels = [one for one in found if "help-panel" in one.classes]
    forms = [
        one
        for one in found
        if one.tag == "form"
        and (
            "decision" in one.classes
            or ("ask" in one.classes and any(one.within(panel) for panel in panels))
        )
    ]
    return [
        one
        for one in found
        if (one.tag == "button" and any(one.within(form) for form in forms))
        or ("problem" in one.classes and any(one.within(panel) for panel in panels))
    ]


OTHER_ASKS: dict[str, str] = {}


@pytest.fixture
def other_asks(tmp_path: pathlib.Path) -> dict[str, str]:
    """The sign-in page, and the page that asks for help about one note, for her and with the
    sign-in off: each holds an ask form outside Help. Rendered by the first test that asks."""
    if OTHER_ASKS:
        return OTHER_ASKS
    for reader in ("her", "open"):
        folder = tmp_path / "other-asks" / reader
        folder.mkdir(parents=True)
        with household_client(reader, folder) as client:
            if reader == "her":
                OTHER_ASKS["sign-in page"] = client.get("/sign-in", headers=PAGE_HEADERS).text
            sign_in_as(client, reader)
            name = waiting_note(
                state_of(client).project_state,
                course="Geometry",
                title="Synthetic questions",
                text="Synthetic note about questions 4 to 8",
            )
            page = client.get(note_help_href(name), headers=PAGE_HEADERS)
            assert page.status_code == 200
            OTHER_ASKS[f"{reader}, note help"] = page.text
    return OTHER_ASKS


def asking_elsewhere(page: str) -> list[Element]:
    """The buttons of each ask form outside Help."""
    found = elements_of(page)
    panels = [one for one in found if "help-panel" in one.classes]
    forms = [
        one
        for one in found
        if one.tag == "form"
        and "ask" in one.classes
        and not any(one.within(panel) for panel in panels)
    ]
    return [one for one in found if one.tag == "button" and any(one.within(form) for form in forms)]


def other_ask_pages(other_asks: dict[str, str]) -> dict[str, str]:
    """The pages with an ask form outside Help, each holding the one button named for it."""
    found = {
        name: [" ".join(one.text.split()) for one in asking_elsewhere(page)]
        for name, page in other_asks.items()
    }
    assert found == {
        "sign-in page": ["Come in"],
        "her, note help": ["Ask for help about this note"],
        "open, note help": ["Ask for help about this note"],
    }
    return other_asks


def padding_changed(sheet: Sheet, ordinary: Sheet, held: list[Element], view: View) -> list[str]:
    """Each side of ``held`` whose padding on ``view`` differs from its padding in ``ordinary``,
    the stylesheet without the padding that gives way."""
    return [
        f"{one.tag}.{'.'.join(sorted(one.classes))} {side} {' '.join(one.text.split())[:40]}"
        for one in held
        for side in ("padding-left", "padding-right")
        if value_of(sheet, one, side, view) != value_of(ordinary, one, side, view)
    ]


def padding_kept(sheet: Sheet, held: list[Element], view: View) -> list[str]:
    """Each side of ``held`` whose padding on ``view`` is not the padding that gives way."""
    return [
        f"{one.tag}.{'.'.join(sorted(one.classes))} {side} {' '.join(one.text.split())[:40]}"
        for one in held
        for side in ("padding-left", "padding-right")
        if value_of(sheet, one, side, view) != GIVES_WAY[one.tag]
    ]


def giving_way_pages(text_pages: dict[str, str], set_apart: dict[str, str]) -> dict[str, str]:
    """The family page with her request's reply form, and her week with a request set apart,
    for her and a parent: the family page holds decision buttons, her week a problem in Help,
    and her own week her Ask as well."""
    pages = family(text_pages) | set_apart
    found = {name: [one.tag for one in giving_way(page)] for name, page in pages.items()}
    for name, tags in found.items():
        mine = name.startswith("her,") or name.endswith("family")
        assert ("button" in tags) == mine, found
        assert ("p" in tags) == name.endswith("her week"), found
    return pages


@pytest.mark.parametrize("view", VIEWS, ids=str)
def test_side_padding_gives_way_in_a_decision_her_ask_and_a_help_problem(
    text_pages: dict[str, str], set_apart_pages: dict[str, str], view: View
) -> None:
    """On a phone with text at 200%, the side padding of these narrows before a word breaks;
    the rule that says so is the one measured in Edge, and nothing later undoes it."""
    css = stylesheet()
    assert css.count(GIVING_WAY) == 1
    sheet = read_sheet(css)
    for name, page in giving_way_pages(text_pages, set_apart_pages).items():
        assert padding_kept(sheet, giving_way(page), view) == [], name


@pytest.mark.parametrize(
    ("becomes", "kept"),
    [
        pytest.param("", {"button", "p"}, id="no-rule"),
        pytest.param(GIVING_WAY.replace(".decision button,\n", ""), {"button"}, id="no-decision"),
        pytest.param(GIVING_WAY.replace(",\n.help-panel .ask button", ""), {"button"}, id="no-ask"),
        pytest.param(GIVING_WAY.split("\n\n")[0], {"p"}, id="no-problem"),
        pytest.param(
            GIVING_WAY + "\n.actions button { padding: 0.6rem 1.35rem; }",
            {"button"},
            id="undone-later",
        ),
    ],
)
def test_the_padding_check_fails_where_a_side_keeps_its_ordinary_padding(
    text_pages: dict[str, str], set_apart_pages: dict[str, str], becomes: str, kept: set[str]
) -> None:
    sheet = broken(GIVING_WAY, becomes)
    pages = giving_way_pages(text_pages, set_apart_pages)
    for view in VIEWS:
        found = {
            one.split(".")[0]
            for page in pages.values()
            for one in padding_kept(sheet, giving_way(page), view)
        }
        assert found == kept, view


@pytest.mark.parametrize("view", VIEWS, ids=str)
def test_an_ask_form_outside_help_keeps_its_ordinary_side_padding(
    other_asks: dict[str, str], view: View
) -> None:
    """The sign-in button and the button that asks about a note keep the side padding the
    stylesheet gives them without the padding that gives way, on every screen."""
    ordinary = broken(GIVING_WAY, "")
    sheet = read_sheet(stylesheet())
    changed = {
        name: padding_changed(sheet, ordinary, asking_elsewhere(page), view)
        for name, page in other_ask_pages(other_asks).items()
    }
    assert changed == {name: [] for name in changed}


ASK_IN_HELP = ".help-panel .ask button"


@pytest.mark.parametrize(
    ("becomes", "reached"),
    [
        pytest.param(
            GIVING_WAY.replace(ASK_IN_HELP, ".ask button"),
            {"sign-in page", "her, note help", "open, note help"},
            id="every-ask",
        ),
        pytest.param(
            GIVING_WAY.replace(ASK_IN_HELP, ".ask > button"), {"sign-in page"}, id="sign-in"
        ),
        pytest.param(
            GIVING_WAY.replace(ASK_IN_HELP, f"{ASK_IN_HELP},\n.ask .actions button"),
            {"her, note help", "open, note help"},
            id="note-help",
        ),
        pytest.param(
            GIVING_WAY + "\n.ask button { padding-inline: 0; }",
            {"sign-in page", "her, note help", "open, note help"},
            id="later-rule",
        ),
    ],
)
def test_the_ordinary_padding_check_fails_where_the_rule_reaches_another_ask(
    text_pages: dict[str, str],
    set_apart_pages: dict[str, str],
    other_asks: dict[str, str],
    becomes: str,
    reached: set[str],
) -> None:
    """Each of these still gives way where it should, so only the check of the other ask
    forms tells them from the rule as written."""
    ordinary = broken(GIVING_WAY, "")
    sheet = broken(GIVING_WAY, becomes)
    pages = other_ask_pages(other_asks)
    for view in VIEWS:
        assert {
            name
            for name, page in pages.items()
            if padding_changed(sheet, ordinary, asking_elsewhere(page), view)
        } == reached, view
        assert {
            name
            for name, page in giving_way_pages(text_pages, set_apart_pages).items()
            if padding_kept(sheet, giving_way(page), view)
        } == set(), view


@pytest.mark.parametrize(
    ("becomes", "short_on"),
    [
        pytest.param("", "right", id="no-widest"),
        pytest.param("  max-width: 100%;\n", "right", id="screen-wide"),
        pytest.param("  max-width: calc(100% - 1rem);\n", "right", id="one-inset"),
        pytest.param("  max-width: calc(100% - 4px);\n", "right", id="pixels"),
        pytest.param(SKIP_WIDTH + "  left: 2px !important;\n", "left", id="hard-left"),
    ],
)
def test_the_skip_check_fails_when_the_outline_can_run_past_an_edge(
    rendered: dict[str, str], becomes: str, short_on: str
) -> None:
    css = stylesheet()
    assert css.count(SKIP_WIDTH) == 1
    css = css.replace(SKIP_WIDTH, becomes)
    for view in VIEWS:
        for page in rendered.values():
            left, right = skip_outline_room(css, page, view)
            assert (left if short_on == "left" else right) < 0, view


# ------------------------------------------------------------- fields, rows and ways back


NEW_NOTE = "/student/homework-notes/new"
SAVE_NOTE = "/student/actions/homework-notes"
SCHOOL_TEXT = (
    "Homework for Wren\n\n- 09/08/2026 - Tuesday\n"
    "08 Geometry - Assigned: PR2 U2 pg. 41 #10, 12, 15: (Due:09/09/2026)\n"
    "08 Geometry - Due: Book Covers:\nCover both books with paper."
)

NOTES: dict[str, str] = {}


@pytest.fixture
def note_pages(tmp_path: pathlib.Path) -> dict[str, str]:
    """Once she has written a homework note, for her, a signed-in parent, and with the sign-in
    off: the note's page; its page for adding details, in her tree and, for a parent and with
    the sign-in off, in the family's; and, for a parent and with the sign-in off, Review
    assignments for a pasted week. Rendered by the first test that asks and kept."""
    if NOTES:
        return NOTES
    for reader in ("her", "parent", "open"):
        folder = tmp_path / "notes" / reader
        folder.mkdir(parents=True)
        with household_client(reader, folder) as client:
            sign_in_as(client, "open" if reader == "open" else "her")
            fields = form_fields(client.get(NEW_NOTE, headers=PAGE_HEADERS).text, SAVE_NOTE)
            written = fields | {"text": "Geometry questions 4-8", "course": "", "due_date": ""}
            assert client.post(SAVE_NOTE, data=written, headers=PAGE_HEADERS).status_code == 303
            name = fields["capture_id"]
            sign_in_as(client, reader)
            NOTES[f"{reader}, note"] = client.get(
                f"/student/homework-notes/{name}", headers=PAGE_HEADERS
            ).text
            for tree in {"her": ("student",), "parent": ("parent",)}.get(
                reader, ("student", "parent")
            ):
                page = client.get(f"/{tree}/homework-notes/{name}/add", headers=PAGE_HEADERS).text
                assert 'id="details-course"' in page, (reader, tree)
                NOTES[f"{reader}, {tree} tree, adding"] = page
            if reader != "her":
                answer = client.post(
                    "/parent/inbox/read", data={"text": SCHOOL_TEXT}, headers=PAGE_HEADERS
                )
                assert answer.status_code == 200
                assert 'class="type-choice"' in answer.text
                NOTES[f"{reader}, review"] = answer.text
    return NOTES


def narrow(view: View) -> bool:
    return one_query_holds("(max-width: 30rem)", view)


CARD_SIDES = [*INSETS, *CARD_EDGES, "max-width"]
SELECT_PADDING = "clamp(0px, 4vw - 0.4rem, 0.85rem)"
"""A select's side padding on a note's details card: Add assignments' share of the screen
with ordinary text, and none once large text and a reader's letter spacing make "Choose a
class" fill the whole width."""


def sits_apart(sheet: Sheet, card: Element, model: Element, view: View) -> list[str]:
    """Each side inset, edge and surface where a note's details card differs from Add
    assignments' card on ``view``, with the side padding of their fields: a select's as a
    browser without the customizable select draws it, which `select_insets_wrong` holds to
    its inset in one with it."""
    found = [
        name
        for name in CARD_SIDES
        if (value_of(sheet, card, name, view) or "none")
        != (value_of(sheet, model, name, view) or "none")
    ]
    theirs = [one for one in elements_of_card(model) if one.tag == "input"]
    for one in elements_of_card(card):
        if one.tag in ("input", "select") and one.attributes.get("type") != "hidden":
            for side in ("padding-left", "padding-right"):
                wanted, seen = value_of(sheet, theirs[-1], side, view), view
                if one.tag == "select":
                    wanted, seen = SELECT_PADDING, without(view)
                if value_of(sheet, one, side, seen) != wanted:
                    found.append(f"{one.tag} {side}")
    return found


def elements_of_card(card: Element) -> list[Element]:
    return [one for one in CARD_ELEMENTS[id(card)] if one.within(card)]


CARD_ELEMENTS: dict[int, list[Element]] = {}


def card_with_elements(page: str, pick: str) -> Element:
    found = elements_of(page)
    card = adding_card_of(found) if pick == "adding" else add_assignments_card_of(found)
    CARD_ELEMENTS[id(card)] = found
    return card


def adding_card_of(found: list[Element]) -> Element:
    (card,) = [one for one in found if "adding-note" in one.classes]
    return card


def add_assignments_card_of(found: list[Element]) -> Element:
    (card,) = [
        one
        for one in found
        if "panel" in one.classes
        and one.parent is not None
        and one.parent.attributes.get("id") == "add-assignments"
    ]
    return card


def signed_px(value: str, view: View) -> float:
    """A length that can be negative, or a ``calc()`` of lengths added and taken away."""
    inner = re.fullmatch(r"calc\((.+)\)", value)
    read = (inner.group(1) if inner else value).strip()
    if inner:
        return calc_px(read, view)
    if zero(read):
        return 0.0
    return (-1.0 if read.startswith("-") else 1.0) * summed_px(read.lstrip("-"), view)


def calc_px(read: str, view: View) -> float:
    terms = re.split(r"\s+([-+])\s+", read)
    total = signed_px(terms[0], view)
    for sign, term in zip(terms[1::2], terms[2::2], strict=True):
        total += signed_px(term, view) if sign == "+" else -signed_px(term, view)
    return total


def type_field_short(sheet: Sheet, page: str, view: View) -> list[str]:
    """Each Type field on Review assignments that does not reach across its card's side
    padding and edge on a narrow ``view``, or whose select keeps a wider side padding than Add
    assignments' fields."""
    found = elements_of(page)
    labels = [one for one in found if one.tag == "label" and "type-choice" in one.classes]
    assert labels
    short = []
    for label in labels:
        (card,) = [one for one in label.ancestors() if one.tag == "article"]
        for side in ("left", "right"):
            reach = -signed_px(value_of(sheet, label, f"margin-{side}", view) or "0", view)
            card_side = padding_px(value_of(sheet, card, f"padding-{side}", view), view)
            card_side += border_px(value_of(sheet, card, f"border-{side}-width", view), view)
            if round(reach, 6) != round(card_side, 6):
                short.append(f"{side} reach {reach} of {card_side}")
        if value_of(sheet, label, "max-width", view) not in (None, "none"):
            short.append("held to a width")
        (select,) = [one for one in found if one.tag == "select" and one.parent is label]
        if value_of(sheet, select, "padding-left", view) != "min(0.85rem, 2vw)":
            short.append("select padding")
    return short


UNREADABLE_PLANS = (
    '<main><section class="panel" id="family-plans"><h2>Plans</h2><article class="draft">'
    '<h3>Wednesday</h3><div class="plan-reading"><ol class="plan-rows"><li class="plan-block">'
    '<a class="assignment-link" href="/x">Canal Era comparison essay</a></li></ol></div>'
    "</article></section></main>"
)
"""Family review's page for plans that can't be read, as `plans_unavailable.html` sets out a
plan's card inside the card of plans."""


def inner_card_insets(sheet: Sheet, view: View) -> list[str]:
    """What the plan's card inside the card of plans keeps of its side insets and edge."""
    (draft,) = [one for one in elements_of(UNREADABLE_PLANS) if one.tag == "article"]
    return [
        name
        for name in CARD_SIDES
        if value_of(sheet, draft, name, view) != CARD_EDGES.get(name)
        and not (
            name.startswith(("margin", "padding")) and zero(value_of(sheet, draft, name, view))
        )
        and not (name.startswith("margin") and value_of(sheet, draft, name, view) is None)
    ]


QUOTED = re.compile(r"\"(?:\\.|[^\"\\])*\"|'(?:\\.|[^'\\])*'")
"""A string in a selector, with its escaped characters, as `style_rules` keeps it whole."""


def sibling_matches(head: str, element: Element, found: list[Element]) -> tuple[Selector, bool]:
    """A selector of the sheet, and whether it matches ``element``: one that names the element
    by the one right before it (``A + B``, with B a single compound) is read here, since the
    shared reader refuses a sibling; a longer one after the ``+`` is refused where it could
    reach the element. A ``+`` inside a string is text, left to the shared reader."""
    bare = QUOTED.sub(lambda text: "_" * len(text[0]), head)
    if "+" not in bare:
        chosen = selector(head)
        return chosen, chosen.matches(element)
    joint = bare.rindex("+")
    left, right = head[:joint].strip(), head[joint + 1 :].strip()
    near, last = selector(left), selector(right)
    whole = Selector(near.parts + last.parts)
    if len(last.parts) > 1:
        if last.matches(element):
            raise UnreadCss(head)
        return whole, False
    before = [
        one
        for one in found
        if one.parent is element.parent and one.position == element.position - 1
    ]
    return whole, last.matches(element) and bool(before) and near.matches(before[0])


def declared_side(
    css: str, element: Element, found: list[Element], box: str, side: str, view: View
) -> str | None:
    """The value the cascade gives one side of a margin or padding on ``element``, from the
    shorthand or the side's own property."""
    order = ("top", "right", "bottom", "left")
    rules = []
    for style in style_rules(css):
        for name, value, important in style.declarations:
            if name == f"{box}-{side}":
                given = value.lower()
            elif name == box:
                given = sides(value.lower())[order.index(side)]
            else:
                continue
            for head in style.selectors.split(","):
                chosen, holds = sibling_matches(unshielded(head), element, found)
                if holds:
                    rules.append(Rule(chosen, given, style.order, style.media, important))
    winning = winner(rules, view)
    return None if winning is None else winning.value


def ways_back_apart(css: str, page: str, view: View) -> list[float]:
    """For each way back that follows another directly in the page's main part, how far
    apart, in pixels, the two links' press areas are on ``view``: the space between their
    lines less what each link's area reaches past its line. Below zero, they overlap."""
    found = elements_of(page)
    (main,) = [one for one in found if one.tag == "main"]
    children = [one for one in found if one.parent is main]
    apart = []
    for before, after in itertools.pairwise(children):
        if not all(one.tag == "p" and "return" in one.classes for one in (before, after)):
            continue
        links = [
            next(one for one in found if one.tag == "a" and one.parent is way)
            for way in (before, after)
        ]
        gap = max(
            signed_px(declared_side(css, before, found, "margin", "bottom", view) or "0", view),
            signed_px(declared_side(css, after, found, "margin", "top", view) or "0", view),
        )
        reach = sum(
            max(0.0, -signed_px(declared_side(css, link, found, "margin", side, view) or "0", view))
            for link, side in ((links[0], "bottom"), (links[1], "top"))
        )
        apart.append(round(gap - reach, 6))
    return apart


@pytest.mark.parametrize("view", VIEWS, ids=str)
def test_a_notes_details_card_sits_as_add_assignments_card_does_on_a_narrow_screen(
    rendered: dict[str, str], note_pages: dict[str, str], view: View
) -> None:
    """On a narrow screen, the card holding a note's class, kind and due date gives up its side
    insets, edge and surface and reaches into the page's side margin as Add assignments' card
    does, and its fields keep the same side padding, so each field is as wide as Add
    assignments' fields and shows its value whole; elsewhere the card keeps its own look."""
    sheet = read_sheet(stylesheet())
    model = card_with_elements(rendered["open, family"], "add assignments")
    adding = {name: page for name, page in note_pages.items() if name.endswith("adding")}
    assert len(adding) == 4
    for name, page in adding.items():
        card = card_with_elements(page, "adding")
        apart = sits_apart(sheet, card, model, view)
        if narrow(view):
            assert apart == [], name
        else:
            assert value_of(sheet, card, "padding-left", view) != "0", name


@pytest.mark.parametrize("view", VIEWS, ids=str)
def test_each_type_field_reaches_across_its_cards_side_padding_on_a_narrow_screen(
    note_pages: dict[str, str], view: View
) -> None:
    sheet = read_sheet(stylesheet())
    for name, page in note_pages.items():
        if name.endswith("review"):
            short = type_field_short(sheet, page, view)
            assert (short == []) == narrow(view), (name, short)


@pytest.mark.parametrize("view", VIEWS, ids=str)
def test_a_plans_card_inside_another_gives_its_rows_the_outer_cards_width(view: View) -> None:
    sheet = read_sheet(stylesheet())
    assert (inner_card_insets(sheet, view) == []) == narrow(view)


@pytest.mark.parametrize("view", VIEWS, ids=str)
def test_review_assignments_save_button_breaks_a_long_word(
    note_pages: dict[str, str], view: View
) -> None:
    sheet = read_sheet(stylesheet())
    for name, page in note_pages.items():
        if name.endswith("review"):
            found = elements_of(page)
            (row,) = [one for one in found if "review-actions" in one.classes]
            buttons = [one for one in found if one.tag == "button" and one.within(row)]
            assert len(buttons) == 2
            assert whole_words(sheet, buttons, view) == [], name


@pytest.mark.parametrize("view", VIEWS, ids=str)
def test_ways_back_one_under_another_never_share_a_press_area(
    note_pages: dict[str, str], text_pages: dict[str, str], view: View
) -> None:
    """A note's page, its page for adding details (three ways back), and a form that couldn't
    be read, whose ways back sit in a box of their own and keep their press areas inside
    their lines."""
    css = stylesheet()
    pairs = 0
    for name, page in (note_pages | text_pages).items():
        apart = ways_back_apart(css, page, view)
        pairs += len(apart)
        assert all(one > 0 for one in apart), (name, apart)
    assert pairs >= 9


@pytest.mark.parametrize("quote", ['"', "'"], ids=["double", "single"])
def test_the_ways_back_check_reads_a_rule_elsewhere_with_a_quoted_mark(
    note_pages: dict[str, str], text_pages: dict[str, str], quote: str
) -> None:
    """A rule for a title holding a mark the reader shields changes the check no more than its
    plain neighbor `[title="ab"]` does."""
    css = stylesheet()
    view = View(320, 16, 32)
    pairs = 0
    for name, page in (note_pages | text_pages).items():
        plain = ways_back_apart(css, page, view)
        pairs += len(plain)
        for mark in ("", ",", ":", ";", "{", "}"):
            rule = f"[title={quote}a{mark}b{quote}] {{ margin: 1rem; }}"
            assert ways_back_apart(f"{css}\n{rule}\n", page, view) == plain, (name, rule)
    assert pairs >= 9


TITLED_WAYS_BACK = (
    '<main><p class="return" title="a,b"><a href="/a" title="e;f">Back to the note</a></p>'
    '<p class="return" title="c:d"><a href="/b" title="g{h}">Back to my week</a></p></main>'
)
"""Two ways back, one under the other, whose paragraphs and links carry titles holding
characters the stylesheet reader shields inside a string."""


@pytest.mark.parametrize(
    ("rule", "apart"),
    [
        pytest.param('.return[title="a,b"] { margin-bottom: 2rem; }', 32.0, id="first"),
        pytest.param(".return[title='a,b'] { margin-bottom: 2rem; }", 32.0, id="single-quotes"),
        pytest.param('.return[title="a;b"] { margin-bottom: 2rem; }', 0.0, id="first-twin"),
        pytest.param('.return + .return[title="c:d"] { margin-top: 2rem; }', 32.0, id="after-plus"),
        pytest.param('.return + .return[title="c,d"] { margin-top: 2rem; }', 0.0, id="after-twin"),
        pytest.param(
            '.return[title="a,b"] + .return { margin-top: 2rem; }', 32.0, id="before-plus"
        ),
        pytest.param('.return[title="a:b"] + .return { margin-top: 2rem; }', 0.0, id="before-twin"),
        pytest.param('a[title="e;f"] { margin-bottom: -0.5rem; }', 24.0, id="first-link"),
        pytest.param('a[title="g{h}"] { margin-top: -0.5rem; }', 24.0, id="second-link"),
        pytest.param('a[title="g}h{"] { margin-top: -0.5rem; }', 32.0, id="second-link-twin"),
    ],
)
def test_the_ways_back_check_reads_a_quoted_mark_as_itself(rule: str, apart: float) -> None:
    """A link's own margin is measured against ways back set 2rem apart."""
    css = f".return, .return a {{ margin: 0; }}\n{rule}"
    if rule.startswith("a["):
        css += "\n.return + .return { margin-top: 2rem; }"
    assert ways_back_apart(css, TITLED_WAYS_BACK, View(320)) == [apart]


def test_the_ways_back_check_refuses_a_stand_in_written_in_a_selector() -> None:
    for stand_in in sorted(STAND_INS):
        with pytest.raises(UnreadCss):
            ways_back_apart(f"p{stand_in}x {{ margin: 1rem; }}", TITLED_WAYS_BACK, View(320))


@pytest.mark.parametrize(
    "head",
    [
        pytest.param('[title="x]+[title=y"]', id="double"),
        pytest.param("[title='x]+[title=y']", id="single"),
        pytest.param('[title="x,]+[title=y"]', id="with-a-mark"),
        pytest.param('[title="x\\"]+[title=y"]', id="escaped-quote"),
    ],
)
def test_the_ways_back_check_reads_a_plus_in_a_string_as_text(head: str) -> None:
    """A ``+`` inside a string joins no siblings: the check refuses it, as `read_sheet` does."""
    page = TITLED_WAYS_BACK.replace("a,b", "x").replace("c:d", "y")
    css = f".return, .return a {{ margin: 0; }}\n{head} {{ margin-top: 2rem; }}"
    with pytest.raises(UnreadCss):
        read_sheet(css.replace("margin-top", "margin-left"))
    with pytest.raises(UnreadCss):
        ways_back_apart(css, page, View(320))


# ------------------------------------------------------------- and each of these fails when broken


ADDING_NOTE_RULE = """  .panel.adding-note {
    margin-inline: min(0px, 4vw - 1.25rem);
    max-width: none;
    padding-inline: 0;
    border: 0;
    border-radius: 0;
    background: none;
    box-shadow: none;
    backdrop-filter: none;
  }"""
ADDING_FIELDS_RULE = """  .adding-note input,
  .adding-note textarea {
    padding-inline: min(0.85rem, 2vw);
  }"""
ADDING_SELECT_RULE = """  .adding-note select {
    padding-inline: clamp(0px, 4vw - 0.4rem, 0.85rem);
  }"""
TYPE_CHOICE_RULE = """  .review-week .type-choice {
    margin-inline: calc(-1.2rem - 1px);
    max-width: none;
  }"""
INNER_CARD_RULE = """  .panel > .draft {
    padding-inline: 0;"""
REVIEW_BUTTONS_RULE = """.review-actions button {
  overflow-wrap: anywhere;
}"""
WAYS_BACK_RULE = """main > .return + .return {
  margin-top: 1.75rem;
}"""


@pytest.mark.parametrize(
    ("was", "becomes"),
    [
        pytest.param(ADDING_NOTE_RULE, "", id="card-keeps-its-look"),
        pytest.param(
            ADDING_NOTE_RULE, ADDING_NOTE_RULE.replace("4vw", "2vw"), id="card-reaches-further"
        ),
        pytest.param(
            ADDING_NOTE_RULE,
            ADDING_NOTE_RULE.replace("    padding-inline: 0;\n", ""),
            id="card-keeps-its-padding",
        ),
        pytest.param(
            ADDING_NOTE_RULE, ADDING_NOTE_RULE.replace("    border: 0;\n", ""), id="card-edge"
        ),
        pytest.param(ADDING_FIELDS_RULE, "", id="fields-padded"),
        pytest.param(ADDING_SELECT_RULE, "", id="selects-padded"),
        pytest.param(
            ADDING_NOTE_RULE, ADDING_NOTE_RULE.replace("    max-width: none;\n", ""), id="held"
        ),
    ],
)
def test_the_details_card_check_fails_when_the_card_or_its_fields_keep_their_insets(
    rendered: dict[str, str], note_pages: dict[str, str], was: str, becomes: str
) -> None:
    sheet = broken(was, becomes)
    model = card_with_elements(rendered["open, family"], "add assignments")
    page = note_pages["open, student tree, adding"]
    assert sits_apart(sheet, card_with_elements(page, "adding"), model, View(320, 16, 32))


@pytest.mark.parametrize(
    "becomes",
    [
        pytest.param("", id="no-reach"),
        pytest.param(TYPE_CHOICE_RULE.replace("1.2rem", "1rem"), id="short-reach"),
        pytest.param(TYPE_CHOICE_RULE.replace(" - 1px", ""), id="edge-left-out"),
        pytest.param(TYPE_CHOICE_RULE.replace("    max-width: none;\n", ""), id="held"),
    ],
)
def test_the_type_field_check_fails_when_the_field_stops_short(
    note_pages: dict[str, str], becomes: str
) -> None:
    sheet = broken(TYPE_CHOICE_RULE, becomes)
    assert type_field_short(sheet, note_pages["open, review"], View(320, 16, 32))


@pytest.mark.parametrize(
    "becomes",
    [
        pytest.param("  .panel > .note {\n    padding-inline: 0;", id="other-element"),
        pytest.param("  .panel > .draft {\n    padding-inline: 1px;", id="padded"),
    ],
)
def test_the_inner_card_check_fails_when_the_plans_card_keeps_its_insets(becomes: str) -> None:
    sheet = broken(INNER_CARD_RULE, becomes)
    assert inner_card_insets(sheet, View(320, 16, 32))


@pytest.mark.parametrize(
    "becomes",
    [
        pytest.param("", id="no-rule"),
        pytest.param(WAYS_BACK_RULE.replace("1.75rem", "1.6rem"), id="touching"),
        pytest.param(WAYS_BACK_RULE.replace("main > ", ".x > "), id="elsewhere"),
    ],
)
def test_the_ways_back_check_fails_when_press_areas_meet(
    note_pages: dict[str, str], becomes: str
) -> None:
    css = stylesheet()
    assert css.count(WAYS_BACK_RULE) == 1
    css = css.replace(WAYS_BACK_RULE, becomes)
    assert min(ways_back_apart(css, note_pages["her, note"], View(320, 16, 32))) <= 0


# ------------------------------------------------------------- a long class name and the way out


CLASS_SELECTS_RULE = """  .adding-note select {
    appearance: base-select;
    overflow-wrap: anywhere;
  }"""
WAY_OUT_RULE = """.review-actions + .return {
  margin-top: 1rem;
}"""


def selects_on_one_line(sheet: Sheet, page: str, view: View) -> list[str]:
    """Each select on a note's details card that keeps a browser's one-line select on a narrow
    ``view``, where a class's name can be wider than the whole line, or that draws itself
    otherwise on a wider ``view``, where the ordinary select stays. On a narrow screen each
    is the customizable select, whose chosen value wraps and breaks a long word."""
    found = elements_of(page)
    card = adding_card_of(found)
    selects = [one for one in found if one.tag == "select" and one.within(card)]
    assert [one.attributes.get("name") for one in selects] == ["course_choice", "kind"]
    kept = []
    for one in selects:
        name = one.attributes["name"]
        drawn = value_of(sheet, one, "appearance", view)
        if not narrow(view):
            if drawn is not None:
                kept.append(f"{name} drawn as {drawn}")
            continue
        if drawn != "base-select":
            kept.append(f"{name} drawn as {drawn}")
        if value_of(sheet, one, "overflow-wrap", view) != "anywhere":
            kept.append(f"{name} keeps a long word whole")
        if value_of(sheet, one, "white-space", view) not in (None, "normal"):
            kept.append(f"{name} white-space")
        if value_of(sheet, one, "text-wrap-mode", view) not in (None, "wrap"):
            kept.append(f"{name} text-wrap-mode")
    return kept


def way_out_apart(css: str, page: str, view: View) -> float:
    """How far apart, in pixels, Review assignments' buttons and the Cancel import link's press
    area are on ``view``: the space between the buttons' row and the link's line less what
    the link's area reaches above its line. At or below zero, they meet."""
    found = elements_of(page)
    (row,) = [one for one in found if "review-actions" in one.classes]
    (way,) = [one for one in found if one.parent is row.parent and one.position == row.position + 1]
    assert way.tag == "p"
    assert "return" in way.classes
    (link,) = [one for one in found if one.tag == "a" and one.parent is way]
    gap = max(
        signed_px(declared_side(css, row, found, "margin", "bottom", view) or "0", view),
        signed_px(declared_side(css, way, found, "margin", "top", view) or "0", view),
    )
    reach = max(
        0.0, -signed_px(declared_side(css, link, found, "margin", "top", view) or "0", view)
    )
    return round(gap - reach, 6)


def test_the_only_select_of_classes_is_on_a_notes_details_card(
    rendered: dict[str, str], note_pages: dict[str, str], text_pages: dict[str, str]
) -> None:
    """Every select on these pages chooses a kind, apart from the one on a note's details
    card that chooses a class, so that card's rule reaches every list of classes."""
    names = set()
    classes = 0
    for name, page in (rendered | note_pages | text_pages).items():
        found = elements_of(page)
        for one in found:
            if one.tag != "select":
                continue
            given = one.attributes.get("name", "")
            names.add(given.split("-")[0])
            if given == "course_choice":
                classes += 1
                assert any("adding-note" in above.classes for above in one.ancestors()), name
    assert names == {"course_choice", "kind"}
    assert classes == 4


@pytest.mark.parametrize("view", VIEWS, ids=str)
def test_a_long_class_name_wraps_in_its_select_on_a_narrow_screen(
    note_pages: dict[str, str], view: View
) -> None:
    """A class's name may be up to 60 characters, far wider than a phone's line with large
    text, and a browser's own select shows one line cut at its edge. On a narrow screen the
    selects on a note's details card are drawn as the customizable select, whose chosen
    value wraps onto as many lines as it needs; elsewhere they stay as they are."""
    sheet = read_sheet(stylesheet())
    adding = {name: page for name, page in note_pages.items() if name.endswith("adding")}
    assert len(adding) == 4
    for name, page in adding.items():
        assert selects_on_one_line(sheet, page, view) == [], name


@pytest.mark.parametrize("view", VIEWS, ids=str)
def test_review_assignments_buttons_and_way_out_never_share_a_press_area(
    note_pages: dict[str, str], view: View
) -> None:
    """Cancel import's press area reaches 0.8rem above its line, so it sits far enough under
    the Save and Edit buttons that the two never meet."""
    css = stylesheet()
    reviews = {name: page for name, page in note_pages.items() if name.endswith("review")}
    assert len(reviews) == 2
    for name, page in reviews.items():
        assert way_out_apart(css, page, view) > 0, name


@pytest.mark.parametrize(
    ("css", "view"),
    [
        pytest.param(
            lambda css: css.replace(CLASS_SELECTS_RULE, ""), View(320, 16, 32), id="no-rule"
        ),
        pytest.param(
            lambda css: css.replace(
                CLASS_SELECTS_RULE, CLASS_SELECTS_RULE.replace("base-select", "auto")
            ),
            View(320, 16, 32),
            id="native",
        ),
        pytest.param(
            lambda css: css.replace(
                CLASS_SELECTS_RULE, CLASS_SELECTS_RULE.replace("anywhere", "normal")
            ),
            View(320, 32, 32),
            id="long-word-kept",
        ),
        pytest.param(
            lambda css: css.replace(
                CLASS_SELECTS_RULE, CLASS_SELECTS_RULE.replace("select {", "input {")
            ),
            View(320, 16, 16),
            id="elsewhere",
        ),
        pytest.param(
            lambda css: css + "\n.adding-note select { white-space: nowrap; }",
            View(320, 16, 32),
            id="nowrap",
        ),
        pytest.param(
            lambda css: css.replace(CLASS_SELECTS_RULE, "") + "\n" + CLASS_SELECTS_RULE,
            View(1440, 16, 16),
            id="every-width",
        ),
    ],
)
def test_the_class_select_check_fails_when_a_select_stays_on_one_line(
    note_pages: dict[str, str], css: Callable[[str], str], view: View
) -> None:
    written = stylesheet()
    assert written.count(CLASS_SELECTS_RULE) == 1
    sheet = read_sheet(css(written))
    assert selects_on_one_line(sheet, note_pages["parent, parent tree, adding"], view)


@pytest.mark.parametrize(
    "becomes",
    [
        pytest.param("", id="no-rule"),
        pytest.param(WAY_OUT_RULE.replace("1rem", "0.8rem"), id="touching"),
        pytest.param(WAY_OUT_RULE.replace(".review-actions", ".actions.x"), id="elsewhere"),
    ],
)
def test_the_way_out_check_fails_when_press_areas_meet(
    note_pages: dict[str, str], becomes: str
) -> None:
    css = stylesheet()
    assert css.count(WAY_OUT_RULE) == 1
    css = css.replace(WAY_OUT_RULE, becomes)
    assert way_out_apart(css, note_pages["parent, review"], View(320, 16, 32)) <= 0


# ------------------------------------------------------------- a wrapping select's inset


CUSTOMIZABLE = "appearance: base-select"
"""The `@supports` test for the customizable select, whose chosen value wraps."""
INSET_RULE = """  @supports (appearance: base-select) {
    .adding-note select {
      padding-inline: clamp(0.4rem, 4vw - 0.4rem, 0.85rem);
    }
  }"""
SPACING_OVERRIDES = """
* { line-height: 1.5 !important; letter-spacing: 0.12em !important;
  word-spacing: 0.16em !important; }
p { margin-bottom: 2em !important; }
"""
"""The text spacing of WCAG 1.4.12, as a reader's own stylesheet sets it."""
DOUBLED_ON_A_PHONE = [
    pytest.param((320, 32, 32), id="the browser's text at 200%"),
    pytest.param((320, 16, 32), id="the page's text at 200%"),
]


def without(view: View, test: str = CUSTOMIZABLE) -> View:
    """``view`` in a browser that fails ``test``."""
    return replace(view, unsupported=view.unsupported | {test})


def clamped_px(value: str | None, view: View) -> float:
    """A padding in pixels, as `padding_px` reads one, or a ``clamp()`` of three lengths,
    each a length or lengths added and taken away."""
    found = re.fullmatch(r"clamp\(([^(),]+),([^(),]+),([^(),]+)\)", value or "")
    if found is None:
        return padding_px(value, view)
    least, wanted, most = (summed_px(one.strip(), view) for one in found.groups())
    return max(0.0, least, min(wanted, most))


def class_selects_of(page: str) -> list[Element]:
    found = elements_of(page)
    card = adding_card_of(found)
    selects = [one for one in found if one.tag == "select" and one.within(card)]
    assert [one.attributes.get("name") for one in selects] == ["course_choice", "kind"]
    return selects


def select_paddings(sheet: Sheet, page: str, view: View) -> list[float]:
    """The left and right padding of each select on a note's details card, in pixels."""
    return [
        clamped_px(value_of(sheet, one, side, view), view)
        for one in class_selects_of(page)
        for side in ("padding-left", "padding-right")
    ]


def select_insets_wrong(sheet: Sheet, page: str, view: View) -> list[str]:
    """Each side of a select on a note's details card whose padding on ``view`` is not as it
    should be. In a browser without the customizable select, a narrow screen keeps
    `SELECT_PADDING`, none once the text is doubled on a phone, so the one-line select fits
    "Choose a class". In one with it, the chosen value wraps, and the padding is the same
    but never under 0.4rem. A wider screen keeps the same padding in both."""
    lacking = without(view)
    wrong = []
    for one in class_selects_of(page):
        for side in ("padding-left", "padding-right"):
            offered = value_of(sheet, one, side, view)
            fallback = value_of(sheet, one, side, lacking)
            name = f"{one.attributes['name']} {side}"
            if not narrow(view):
                if offered != fallback:
                    wrong.append(f"{name} {offered}, {fallback} without the customizable select")
                continue
            if fallback != SELECT_PADDING:
                wrong.append(f"{name} {fallback} without the customizable select")
            least = max(0.4 * view.root_text, clamped_px(fallback, view))
            if round(clamped_px(offered, view), 6) != round(least, 6):
                wrong.append(f"{name} {clamped_px(offered, view)} px, not {least}")
    return wrong


@pytest.mark.parametrize("spacing", ["", SPACING_OVERRIDES], ids=["", "spacing overrides"])
@pytest.mark.parametrize("sizes", DOUBLED_ON_A_PHONE)
def test_a_class_select_keeps_a_small_inset_with_doubled_text_on_a_phone(
    note_pages: dict[str, str], sizes: tuple[int, int, int], spacing: str
) -> None:
    """With the text doubled on a 320 px screen, a browser that offers the customizable
    select keeps 0.4rem, 12.8 px, between each select's border and its value, which wraps;
    one without it keeps no side padding, so its one-line select fits "Choose a class". A
    reader's text spacing changes neither."""
    view = View(*sizes)
    sheet = read_sheet(stylesheet() + spacing)
    adding = {name: page for name, page in note_pages.items() if name.endswith("adding")}
    assert len(adding) == 4
    for name, page in adding.items():
        assert select_paddings(sheet, page, view) == [12.8] * 4, name
        assert select_paddings(sheet, page, without(view)) == [0.0] * 4, name


@pytest.mark.parametrize("view", VIEWS, ids=str)
def test_a_class_select_keeps_its_padding_but_never_under_a_small_inset_where_its_value_wraps(
    note_pages: dict[str, str], view: View
) -> None:
    sheet = read_sheet(stylesheet())
    adding = {name: page for name, page in note_pages.items() if name.endswith("adding")}
    for name, page in adding.items():
        assert select_insets_wrong(sheet, page, view) == [], name


@pytest.mark.parametrize(
    ("css", "sizes"),
    [
        pytest.param(lambda css: css.replace(INSET_RULE, ""), (320, 32, 32), id="no-inset"),
        pytest.param(
            lambda css: css.replace(INSET_RULE, INSET_RULE.replace("(appear", "not (appear")),
            (320, 32, 32),
            id="turned-around",
        ),
        pytest.param(
            lambda css: css.replace(INSET_RULE, INSET_RULE.replace("base-select)", "auto)")),
            (320, 32, 32),
            id="another-test",
        ),
        pytest.param(
            lambda css: css.replace(
                INSET_RULE,
                "  .adding-note select {\n"
                "    padding-inline: clamp(0.4rem, 4vw - 0.4rem, 0.85rem);\n  }",
            ),
            (320, 32, 32),
            id="every-browser",
        ),
        pytest.param(
            lambda css: css.replace(INSET_RULE, INSET_RULE.replace("(0.4rem", "(0.2rem")),
            (320, 16, 32),
            id="smaller",
        ),
        pytest.param(
            lambda css: css.replace(
                INSET_RULE, INSET_RULE.replace("clamp(0.4rem, 4vw - 0.4rem, 0.85rem)", "0.4rem")
            ),
            (820, 32, 32),
            id="flat",
        ),
        pytest.param(
            lambda css: css.replace(INSET_RULE, INSET_RULE.replace("-inline", "-left")),
            (320, 32, 32),
            id="one-side",
        ),
        pytest.param(
            lambda css: css.replace(INSET_RULE, INSET_RULE.replace("select {", "input {")),
            (320, 32, 32),
            id="elsewhere",
        ),
        pytest.param(
            lambda css: css.replace(INSET_RULE, "").replace(
                ADDING_SELECT_RULE, INSET_RULE + "\n\n" + ADDING_SELECT_RULE
            ),
            (320, 32, 32),
            id="overridden",
        ),
        pytest.param(
            lambda css: css.replace(INSET_RULE, "") + "\n" + INSET_RULE,
            (1440, 16, 16),
            id="every-width",
        ),
        pytest.param(
            lambda css: css.replace(ADDING_SELECT_RULE, ""), (320, 32, 32), id="no-fallback"
        ),
    ],
)
def test_the_select_inset_check_fails_when_the_inset_is_lost_or_reaches_too_far(
    note_pages: dict[str, str], css: Callable[[str], str], sizes: tuple[int, int, int]
) -> None:
    written = stylesheet()
    assert written.count(INSET_RULE) == 1
    assert written.count(ADDING_SELECT_RULE) == 1
    sheet = read_sheet(css(written))
    assert select_insets_wrong(sheet, note_pages["parent, parent tree, adding"], View(*sizes))


SUPPORTS_SHEET = "p {{ flex-wrap: nowrap; }}\n{} {{ p {{ flex-wrap: wrap; }} }}"


def supports_read(css: str, view: View) -> str | None:
    (one,) = [one for one in elements_of("<main><p>a</p></main>") if one.tag == "p"]
    return value_of(read_sheet(css), one, "flex-wrap", view)


@pytest.mark.parametrize(
    ("prelude", "applies"),
    [
        ("@supports (appearance: base-select)", True),
        ("@supports not (appearance: base-select)", False),
        ("@supports (appearance: auto)", True),
        ("@supports not (appearance: auto)", False),
        ("@supports (appearance: none)", True),
        ("@supports not (appearance: none)", False),
        ("@supports (appearance: banana)", False),
        ("@supports not (appearance: banana)", True),
        ("@supports (overflow-wrap: anywhere)", True),
        ("@supports not (overflow-wrap: anywhere)", False),
        ("@supports (overflow-wrap: banana)", False),
        ("@supports not (overflow-wrap: banana)", True),
        ("@supports (APPEARANCE: BASE-SELECT)", True),
        ("@SUPPORTS (appearance: base-select)", True),
        ("@supports NOT (appearance: banana)", True),
        ("@supports (appearance:base-select)", True),
        ("@supports ( appearance : base-select )", True),
        ("@supports(appearance: base-select)", True),
        ("@supports\tnot\n(appearance:\tbanana)", True),
        ("@supports /* a */ (appearance: /* b */ base-select)", True),
        ("@supports not/**/(appearance: banana)", True),
    ],
)
def test_the_resolver_reads_a_supports_test_as_a_browser_reads_it(
    prelude: str, applies: bool
) -> None:
    """Edge 154's answer for each test and each way of writing it."""
    css = SUPPORTS_SHEET.format(prelude)
    assert supports_read(css, View(320)) == ("wrap" if applies else "nowrap")


@pytest.mark.parametrize(
    ("css", "width", "applies"),
    [
        pytest.param(
            "p { flex-wrap: nowrap; } @media (max-width: 30rem) { @supports (appearance: "
            "base-select) { p { flex-wrap: wrap; } } }",
            320,
            True,
            id="narrow",
        ),
        pytest.param(
            "p { flex-wrap: nowrap; } @media (max-width: 30rem) { @supports (appearance: "
            "base-select) { p { flex-wrap: wrap; } } }",
            1440,
            False,
            id="wide",
        ),
        pytest.param(
            "p { flex-wrap: nowrap; } @media (max-width: 30rem) { @supports not (appearance: "
            "base-select) { p { flex-wrap: wrap; } } }",
            320,
            False,
            id="narrow-not",
        ),
        pytest.param(
            "p { flex-wrap: nowrap; } @media (max-width: 30rem) { @supports (appearance: "
            "base-select) { p { flex-wrap: wrap; } } p { flex-wrap: nowrap; } }",
            320,
            False,
            id="later-rule",
        ),
    ],
)
def test_the_resolver_reads_a_supports_test_inside_a_media_query_as_a_browser_reads_it(
    css: str, width: int, applies: bool
) -> None:
    assert supports_read(css, View(width)) == ("wrap" if applies else "nowrap")


@pytest.mark.parametrize(
    ("prelude", "applies"),
    [
        ("@supports (appearance: base-select)", False),
        ("@supports not (appearance: base-select)", True),
        ("@supports (appearance: auto)", True),
        ("@supports not (appearance: banana)", True),
    ],
)
def test_the_resolver_reads_a_supports_test_in_a_browser_that_fails_it(
    prelude: str, applies: bool
) -> None:
    """A view whose browser fails ``appearance: base-select`` stands for one without the
    customizable select; every other test keeps Edge's answer."""
    css = SUPPORTS_SHEET.format(prelude)
    lacking = View(320, unsupported=frozenset({CUSTOMIZABLE, "appearance: banana"}))
    assert supports_read(css, lacking) == ("wrap" if applies else "nowrap")


@pytest.mark.parametrize(
    "css",
    [
        pytest.param(
            SUPPORTS_SHEET.format("@supports (appearance: base-select) and (appearance: none)"),
            id="and",
        ),
        pytest.param(
            SUPPORTS_SHEET.format("@supports (appearance: banana) or (appearance: base-select)"),
            id="or",
        ),
        pytest.param(SUPPORTS_SHEET.format("@supports selector(select)"), id="selector"),
        pytest.param(
            SUPPORTS_SHEET.format("@supports ((appearance: base-select))"), id="doubled-brackets"
        ),
        pytest.param(
            SUPPORTS_SHEET.format("@supports not (not (appearance: base-select))"), id="not-not"
        ),
        pytest.param(
            SUPPORTS_SHEET.format("@supports (appearance: base-select !important)"),
            id="important",
        ),
        pytest.param(SUPPORTS_SHEET.format("@supports"), id="empty"),
        pytest.param(SUPPORTS_SHEET.format("@supports (--x: y)"), id="custom-property"),
        pytest.param(SUPPORTS_SHEET.format("@supports (display: grid)"), id="unknown-test"),
        pytest.param(SUPPORTS_SHEET.format("@supports (width: 1px)"), id="length"),
        pytest.param(SUPPORTS_SHEET.format('@supports (appearance: "base-select")'), id="string"),
        pytest.param(SUPPORTS_SHEET.format("@supports (appe\\61rance: base-select)"), id="escape"),
        pytest.param(
            SUPPORTS_SHEET.format("@supports not(appearance: base-select)"), id="not-function"
        ),
        pytest.param(
            SUPPORTS_SHEET.format("@supports not(appearance: banana)"), id="not-function-fails"
        ),
        pytest.param(
            "p { flex-wrap: nowrap; } @supports (appearance: base-select) { @supports "
            "(overflow-wrap: anywhere) { p { flex-wrap: wrap; } } }",
            id="inside-supports",
        ),
        pytest.param(
            "p { flex-wrap: nowrap; } @supports (appearance: base-select) { @media "
            "(max-width: 30rem) { p { flex-wrap: wrap; } } }",
            id="media-inside",
        ),
        pytest.param(
            "p { flex-wrap: nowrap; } @supports (appearance: base-select) { p { & { "
            "flex-wrap: wrap; } } }",
            id="nested-rule",
        ),
        pytest.param(
            "p { flex-wrap: nowrap; } @media (hover: hover) { @supports not (appearance: "
            "base-select) { p { flex-wrap: wrap; } } }",
            id="unknown-media-around-a-failing-test",
        ),
    ],
)
def test_the_resolver_refuses_a_supports_condition_it_does_not_read(css: str) -> None:
    with pytest.raises(UnreadCss):
        supports_read(css, View(320))


def test_the_resolver_refuses_a_view_failing_a_test_it_does_not_know() -> None:
    css = SUPPORTS_SHEET.format("@supports (appearance: base-select)")
    assert supports_read(css, View(320)) == "wrap"
    with pytest.raises(UnreadCss):
        supports_read(css, View(320, unsupported=frozenset({"display: grid"})))
    wide = (
        "p { flex-wrap: nowrap; } @media (min-width: 72rem) { @supports (appearance: "
        "base-select) { p { flex-wrap: wrap; } } }"
    )
    assert supports_read(wide, View(320)) == "nowrap"
    with pytest.raises(UnreadCss):
        supports_read(wide, View(320, unsupported=frozenset({"display: grid"})))
