"""The masthead and the family page's Add assignments forms on a narrow screen with large
text, worked out from the stylesheet and the pages as rendered.

At 320 pixels with text at 200%, the wordmark, the page links, and the sign-out control
are wider together than the line, and an entry column of 13rem is wider than the card
that holds it. The page links wrap onto as many lines as they need at every width; where
the masthead is a column, the brand stays within its line and the wordmark goes under
the mark when the two do not fit it; the entry
columns are never wider than the form; and a label or button in Add assignments breaks a
word only when that word cannot fit its line. On a screen where everything fits, each of
these leaves the layout as it is.

No browser runs in these tests, so the cascade is resolved here, for the part of CSS
these elements depend on: compound selectors of a type, an id, classes, attributes and
pseudo-classes, with descendant and child combinators, in comma lists; importance, then
specificity, then source order; and inheritance for the properties a browser inherits. A
rule inside a media query counts only where the query holds. A media query reads its `rem`
from the browser's own text size, so large text is checked two ways: the browser's text size
doubled, which moves the narrow layout's breakpoint with it, and the page's own text
doubled, which leaves the breakpoint where it is. A media feature the resolver does not
know is refused, never assumed either way.
"""

import functools
import pathlib
import re
from dataclasses import dataclass, field, replace
from html.parser import HTMLParser

import pytest

from blossom.settings import REPOSITORY_ROOT
from tests.support import HER_PAGE, PAGE_HEADERS, household_client, sign_in_as

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

INHERITED = frozenset({"overflow-wrap", "white-space", "font-size"})
NEVER_ON = frozenset({"hover", "focus", "focus-visible", "focus-within", "active", "visited"})


@dataclass(frozen=True)
class View:
    """One reader's screen: its width, the browser's text size, and the page's root size."""

    width: int
    browser_text: int = 16
    root_text: int = 16


VIEWS = [View(width, browser, root) for width in WIDTHS for browser, root in TEXT_SIZES.values()]


class UnreadCss(AssertionError):
    """A part of the stylesheet this resolver cannot evaluate. Raised, never guessed at."""


# ------------------------------------------------------------- the pages, as elements


@dataclass(eq=False)
class Element:
    """An element of a page, the same only as itself."""

    tag: str
    attributes: dict[str, str]
    parent: "Element | None"
    text: str = ""

    @property
    def classes(self) -> frozenset[str]:
        return frozenset(self.attributes.get("class", "").split())

    def ancestors(self) -> list["Element"]:
        found, above = [], self.parent
        while above is not None:
            found.append(above)
            above = above.parent
        return found

    def within(self, test: "Element") -> bool:
        return self is test or any(above is test for above in self.ancestors())


class Elements(HTMLParser):
    """Every element of a page, each with the chain of elements above it."""

    VOID = frozenset({"input", "br", "img", "meta", "link", "hr", "source"})

    def __init__(self) -> None:
        super().__init__()
        self.open: list[Element] = []
        self.found: list[Element] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        element = Element(
            tag, {name: value or "" for name, value in attrs}, self.open[-1] if self.open else None
        )
        self.found.append(element)
        if tag not in self.VOID:
            self.open.append(element)

    def handle_endtag(self, tag: str) -> None:
        for index in range(len(self.open) - 1, -1, -1):
            if self.open[index].tag == tag:
                del self.open[index:]
                return

    def handle_data(self, data: str) -> None:
        if self.open:
            self.open[-1].text += data


def elements_of(page: str) -> list[Element]:
    parser = Elements()
    parser.feed(page)
    return parser.found


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


def fold_holds(fold: Element, one: Element) -> bool:
    return any(above is fold for above in one.ancestors())


# ------------------------------------------------------------- the stylesheet, as rules


@dataclass(frozen=True)
class Compound:
    tag: str | None = None
    ids: frozenset[str] = frozenset()
    classes: frozenset[str] = frozenset()
    attributes: tuple[tuple[str, str | None], ...] = ()
    negated: tuple["Compound", ...] = ()
    never: bool = False
    states: int = 0
    root: bool = False

    @property
    def specificity(self) -> tuple[int, int, int]:
        inner = [item.specificity for item in self.negated]
        return (
            len(self.ids) + sum(i[0] for i in inner),
            len(self.classes) + len(self.attributes) + self.states + sum(i[1] for i in inner),
            (1 if self.tag else 0) + sum(i[2] for i in inner),
        )

    def matches(self, element: Element) -> bool:
        if self.never or (self.root and element.tag != "html"):
            return False
        if self.tag is not None and self.tag != element.tag:
            return False
        if self.ids and {element.attributes.get("id")} != set(self.ids):
            return False
        if not self.classes <= element.classes:
            return False
        for name, wanted in self.attributes:
            if name not in element.attributes:
                return False
            if wanted is not None and element.attributes[name] != wanted:
                return False
        return not any(item.matches(element) for item in self.negated)


PIECE = re.compile(
    r"""(?P<not>:not\((?P<inner>[^()]*)\))
      | (?P<id>\#[\w-]+)
      | (?P<class>\.[\w-]+)
      | (?P<attribute>\[(?P<name>[\w-]+)(?:=["']?(?P<value>[^"'\]]*)["']?)?\])
      | (?P<pseudo>::?[\w-]+(?:\([^()]*\))?)
      | (?P<tag>[a-zA-Z][\w-]*|\*)""",
    re.VERBOSE,
)


def compound(text: str) -> Compound:
    """One compound selector. A pseudo-element, or a state a page at rest is never in, never
    matches; any other pseudo-class is refused."""
    tag: str | None = None
    ids: set[str] = set()
    classes: set[str] = set()
    attributes: list[tuple[str, str | None]] = []
    negated: list[Compound] = []
    never = False
    states = 0
    root = False
    position = 0
    while position < len(text):
        piece = PIECE.match(text, position)
        if piece is None:
            raise UnreadCss(text)
        position = piece.end()
        if piece["not"]:
            negated.append(compound(piece["inner"].strip()))
        elif piece["id"]:
            ids.add(piece["id"][1:])
        elif piece["class"]:
            classes.add(piece["class"][1:])
        elif piece["attribute"]:
            attributes.append((piece["name"], piece["value"]))
        elif piece["pseudo"]:
            name = piece["pseudo"].lstrip(":")
            if piece["pseudo"].startswith("::") or name in NEVER_ON:
                never = True
            elif name not in ("root", "link"):
                raise UnreadCss(text)
            else:
                states += 1
                root = root or name == "root"
        elif piece["tag"] and piece["tag"] != "*":
            tag = piece["tag"].lower()
    return Compound(
        tag,
        frozenset(ids),
        frozenset(classes),
        tuple(attributes),
        tuple(negated),
        never,
        states,
        root,
    )


@dataclass(frozen=True)
class Selector:
    parts: tuple[tuple[str, Compound], ...]

    @property
    def specificity(self) -> tuple[int, int, int]:
        each = [item.specificity for _, item in self.parts]
        return (sum(i[0] for i in each), sum(i[1] for i in each), sum(i[2] for i in each))

    def matches(self, element: Element) -> bool:
        def climb(index: int, at: Element) -> bool:
            joiner, item = self.parts[index]
            if not item.matches(at):
                return False
            if index == 0:
                return True
            above = at.ancestors()
            reach = above[:1] if joiner == ">" else above
            return any(climb(index - 1, candidate) for candidate in reach)

        return climb(len(self.parts) - 1, element)


def selector(text: str) -> Selector:
    parts: list[tuple[str, Compound]] = []
    joiner = " "
    for piece in re.findall(r"[>+~]|[^\s>+~]+", text.strip()):
        if piece in ("+", "~"):
            raise UnreadCss(text)
        if piece == ">":
            joiner = ">"
            continue
        parts.append((joiner, compound(piece)))
        joiner = " "
    return Selector(tuple(parts))


WATCHED = frozenset(
    {
        "align-items",
        "align-self",
        "display",
        "flex-direction",
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
    }
)
"""The properties these checks read. A rule that sets none of them is passed over unread."""


@dataclass(frozen=True)
class Rule:
    chosen: Selector
    value: str
    order: int
    media: str | None
    important: bool = False


@dataclass
class Sheet:
    rules: dict[str, list[Rule]] = field(default_factory=dict)
    """Each watched property's rules, in source order."""
    matched: dict[tuple[Element, str], list[Rule]] = field(default_factory=dict)
    """The rules that reach each element, by property, whatever the screen. Keyed on the
    element itself, which keeps it alive while the sheet is in use."""


def blocks(css: str) -> list[tuple[str, str]]:
    """Each outermost block of ``css`` as what comes before its brace and what is inside."""
    found: list[tuple[str, str]] = []
    depth, after, opened = 0, 0, 0
    for index, character in enumerate(css):
        if character == "{":
            if depth == 0:
                opened = index
            depth += 1
        elif character == "}":
            depth -= 1
            if depth == 0:
                found.append((css[after:opened].strip(), css[opened + 1 : index]))
                after = index + 1
    return found


LOGICAL = {"inline-size": "width", "min-inline-size": "min-width", "max-inline-size": "max-width"}
"""Each logical width and the width it sets in a page written left to right."""


def longhands(name: str, value: str) -> dict[str, str]:
    """What one declaration sets among the watched properties: ``font`` sets the text size,
    ``flex-flow`` the direction and the wrapping, a logical width its width, and
    ``place-items`` or ``place-self`` the alignment across a column with its first word."""
    if name == "font":
        return {"font-size": value}
    if name in LOGICAL:
        return {LOGICAL[name]: value}
    if name in ("place-items", "place-self"):
        return {name.replace("place", "align"): value.split()[0]}
    if name == "flex-flow":
        found = {}
        for word in value.split():
            if word in ("wrap", "nowrap", "wrap-reverse"):
                found["flex-wrap"] = word
            elif word in ("row", "row-reverse", "column", "column-reverse"):
                found["flex-direction"] = word
            else:
                raise UnreadCss(value)
        return found
    return {name: value} if name in WATCHED else {}


IMPORTANT = re.compile(r"\s*!\s*important$")


def read_sheet(css: str) -> Sheet:
    """Every rule of ``css`` that sets a watched property, in source order, each with the
    media condition it sits under. Names and keywords are read in any case, as a browser
    reads them. Font faces and keyframes hold no rules for elements and are passed over; a
    rule nested inside another is refused."""
    sheet = Sheet()
    plain = re.sub(r"/\*.*?\*/", "", css, flags=re.S)
    order = 0

    def read(part: str, media: str | None) -> None:
        nonlocal order
        for before, inside in blocks(part):
            if before.startswith(("@font-face", "@keyframes")):
                continue
            if before.startswith("@media"):
                if media is not None:
                    raise UnreadCss(before)
                read(inside, before.removeprefix("@media").strip())
                continue
            if before.startswith("@") or "{" in inside:
                raise UnreadCss(before)
            order += 1
            declared: dict[str, tuple[str, bool]] = {}
            for line in inside.split(";"):
                name, _, value = line.partition(":")
                value, important = IMPORTANT.subn("", value.strip().lower())
                if not value:
                    continue
                for longhand, setting in longhands(name.strip().lower(), value).items():
                    if important or not declared.get(longhand, ("", False))[1]:
                        declared[longhand] = (setting, important > 0)
            if not declared:
                continue
            chosen = [selector(head) for head in before.split(",")]
            for name, (value, weight) in declared.items():
                for one in chosen:
                    sheet.rules.setdefault(name, []).append(Rule(one, value, order, media, weight))

    read(plain, None)
    return sheet


def media_length(value: str, view: View) -> float:
    found = re.fullmatch(r"(\d*\.?\d+)(px|rem|em)", value.strip())
    if found is None:
        raise UnreadCss(value)
    return float(found.group(1)) * (1 if found.group(2) == "px" else view.browser_text)


def holds(condition: str | None, view: View) -> bool:
    """Whether a media condition holds on ``view``; a list holds when any query does."""
    if condition is None:
        return True
    return any(one_query_holds(query.strip(), view) for query in condition.split(","))


def one_query_holds(query: str, view: View) -> bool:
    result = True
    for word in re.sub(r"\([^)]*\)", " ", query).split():
        if word == "print":
            result = False
        elif word not in ("and", "only", "screen", "all"):
            raise UnreadCss(query)
    features = re.findall(r"\(\s*([\w-]+)\s*:\s*([^)]+?)\s*\)", query)
    if len(features) != query.count("("):
        raise UnreadCss(query)
    for name, value in features:
        if name == "min-width":
            result = result and view.width >= media_length(value, view)
        elif name == "max-width":
            result = result and view.width <= media_length(value, view)
        elif name == "prefers-reduced-motion" and value in ("reduce", "no-preference"):
            result = result and value == "no-preference"
        else:
            raise UnreadCss(query)
    return result


def declared_for(sheet: Sheet, element: Element, name: str, view: View) -> Rule | None:
    """The rule whose value for ``name`` the cascade settles on for ``element`` on ``view``,
    the element's own or, for an inherited property, the nearest ancestor's."""
    key = (element, name)
    if key not in sheet.matched:
        sheet.matched[key] = [
            rule for rule in sheet.rules.get(name, []) if rule.chosen.matches(element)
        ]
    winner: Rule | None = None
    for rule in sheet.matched[key]:
        if holds(rule.media, view) and (
            winner is None
            or (rule.important, rule.chosen.specificity, rule.order)
            > (winner.important, winner.chosen.specificity, winner.order)
        ):
            winner = rule
    if winner is None and name in INHERITED and element.parent is not None:
        return declared_for(sheet, element.parent, name, view)
    return winner


def value_of(sheet: Sheet, element: Element, name: str, view: View) -> str | None:
    rule = declared_for(sheet, element, name, view)
    return None if rule is None else rule.value


def stylesheet() -> str:
    return (REPOSITORY_ROOT / "blossom" / "static" / "blossom.css").read_text(encoding="utf-8")


# ------------------------------------------------------------- what each rule must do


def links_wrap(sheet: Sheet, page: str, view: View) -> bool:
    """The page links and the sign-out control are each a whole item of a flex line that
    wraps, so a control that does not fit goes to a line of its own."""
    nav = [one for one in masthead_of(page) if one.tag == "nav" and "places" in one.classes]
    assert len(nav) == 1
    return (
        value_of(sheet, nav[0], "display", view) == "flex"
        and value_of(sheet, nav[0], "flex-wrap", view) == "wrap"
    )


def brand_wraps_where_it_should(sheet: Sheet, page: str, view: View) -> bool:
    """The brand is a row of the mark and the wordmark. Where the masthead is a column, it is
    a flex line that wraps and stays within the masthead's line; where the masthead is a
    row, it keeps one line, so a wide screen keeps its masthead."""
    head = masthead_of(page)
    brand = [one for one in head if one.tag == "a" and "brand" in one.classes]
    assert len(brand) == 1
    assert [one.tag for one in head if one.parent is brand[0]] == ["img", "span"]
    wrapping = value_of(sheet, brand[0], "flex-wrap", view)
    along = value_of(sheet, brand[0], "flex-direction", view) in (None, "row")
    if value_of(sheet, head[0], "flex-direction", view) != "column":
        return along and wrapping in (None, "nowrap")
    return (
        along
        and value_of(sheet, brand[0], "display", view) in ("flex", "inline-flex")
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
    """What decides the brand's width across a column masthead, as the cascade settles it:
    whether it wraps, its width and the least and most it may be, and whether the masthead
    stretches it across the line."""

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
the brand holds for each."""


@functools.cache
def keeps_within_the_line(sizing: Sizing) -> bool:
    """For every line, mark, gap and wordmark, the wordmarks that fill the line exactly and a
    pixel either side included, the wordmark goes under the mark exactly when the two are
    wider together than the line. A brand held wider than its line, or narrower, gets some
    of these wrong."""
    for line in LINES:
        for mark in MARKS:
            for gap in GAPS:
                fill = line - mark - gap
                for word in (*WORDS, fill - 1, fill, fill + 1):
                    if word <= 0:
                        continue
                    _, lines = laid_out(sizing, line, mark, gap, word)
                    if (lines == 2) != (mark + gap + word > line):
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


ADDING_CONTROLS = [
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
    """The labels and buttons of Add assignments whose words do not break when one is wider
    than its line. `anywhere` also lets the control be narrower than its longest word,
    which `break-word` does not, so a button inside the card never pushes past it."""
    found = [one for one in adding_of(page) if one.tag in ("label", "button")]
    assert [(one.tag, " ".join(one.text.split())) for one in found] == ADDING_CONTROLS
    return [
        " ".join(one.text.split())
        for one in found
        if value_of(sheet, one, "overflow-wrap", view) != "anywhere"
    ]


CUT = {
    "white-space": ("nowrap", "pre"),
    "overflow": ("hidden", "clip", "auto", "scroll"),
    "overflow-x": ("hidden", "clip", "auto", "scroll"),
    "text-overflow": ("ellipsis", "clip"),
}


def cut_or_shrunk(sheet: Sheet, held: list[Element]) -> list[str]:
    """Each element that some screen keeps on one line, cuts off, or gives a text size of
    its own that another screen does not. A visually hidden word is clipped by design and
    is passed over."""
    found = []
    for one in held:
        if "visually-hidden" in one.classes:
            continue
        sizes = {
            getattr(declared_for(sheet, one, "font-size", view), "order", None) for view in VIEWS
        }
        if len(sizes) > 1:
            found.append(f"{one.tag}.{'.'.join(sorted(one.classes))} font-size")
        for name, refused in CUT.items():
            for view in VIEWS:
                if value_of(sheet, one, name, view) in refused:
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
        "p:focus-within, p:first-child { flex-wrap: wrap; }",
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
    assert value_of(spelled, other, "max-width", phone) == "3px"
    assert compound(":root").specificity == (0, 1, 0)
    assert compound("html:root").specificity == (0, 1, 1)
    assert not compound(":root").matches(other)
    assert (
        floor_px("repeat(auto-fit, minmax(min(13rem, 100%), 1fr))", 161, View(320, 16, 32)) == 161
    )
    assert floor_px("repeat(auto-fit, minmax(13rem, 1fr))", 161, View(320, 16, 32)) == 416


@pytest.mark.parametrize("view", VIEWS, ids=str)
def test_the_page_links_and_sign_out_wrap_onto_lines_of_their_own(
    rendered: dict[str, str], view: View
) -> None:
    """For her, a signed-in parent, and with the sign-in off, on every screen: the links and
    the sign-out control wrap rather than run past the edge. On a tablet with the page's
    text at 200%, the brand and three controls fit a row only by wrapping, so this holds on
    wide screens too, where everything fits one line and nothing moves."""
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
    rendered: dict[str, str], view: View
) -> None:
    """Both forms, the pasted text's and the one assignment's."""
    sheet = read_sheet(stylesheet())
    for name, page in family(rendered).items():
        assert words_break_where_they_must(sheet, page, view) == [], name


def test_nothing_in_the_masthead_or_the_forms_is_cut_or_shrunk_on_any_screen(
    rendered: dict[str, str],
) -> None:
    """No element of the masthead or of the Add assignments forms is held to one line, cut
    off, or given a smaller text size on a narrow screen."""
    sheet = read_sheet(stylesheet())
    for name, page in rendered.items():
        held = masthead_of(page) + (adding_of(page) if name.endswith("family") else [])
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
ADDING_RULE = """#add-assignments label,
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


@pytest.mark.parametrize(
    ("becomes", "missed"),
    [
        pytest.param("", [name for _, name in ADDING_CONTROLS], id="no-rule"),
        pytest.param(
            ADDING_RULE.replace("anywhere", "break-word"),
            [name for _, name in ADDING_CONTROLS],
            id="break-word",
        ),
        pytest.param(
            ADDING_RULE.replace("#add-assignments", ".entry"),
            ["School text", "Preview assignments"],
            id="entry-form-only",
        ),
        pytest.param(
            ADDING_RULE.replace("#add-assignments label,\n", ""),
            [name for tag, name in ADDING_CONTROLS if tag == "label"],
            id="buttons-only",
        ),
        pytest.param(
            ADDING_RULE.replace(",\n#add-assignments button", ""),
            [name for tag, name in ADDING_CONTROLS if tag == "button"],
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


@pytest.mark.parametrize(
    "added",
    [
        ".places a { white-space: nowrap; }",
        "@media (max-width: 30rem) { .wordmark { font-size: 1.2rem; } }",
        ".masthead { overflow-x: hidden; }",
        ".entry label { text-overflow: ellipsis; }",
        "@media (max-width: 30rem) { #add-assignments button { font-size: 0.8rem; } }",
    ],
)
def test_the_cut_check_fails_when_a_rule_holds_cuts_or_shrinks_a_line(
    rendered: dict[str, str], added: str
) -> None:
    sheet = read_sheet(stylesheet() + "\n" + added + "\n")
    found = [
        problem
        for name, page in rendered.items()
        for problem in cut_or_shrunk(
            sheet, masthead_of(page) + (adding_of(page) if name.endswith("family") else [])
        )
    ]
    assert found
