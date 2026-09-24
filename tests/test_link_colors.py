"""The color a link ends up with, and the height of the link to an assignment's details,
worked out from the stylesheet and the pages as rendered.

No browser runs in these tests, so the cascade is resolved here, for the part
of CSS the stylesheet uses on links: compound selectors of a type, classes,
attributes, and pseudo-classes, with descendant and child combinators, in
comma lists; specificity, then source order; `:visited` and `:link` by the
state asked for, and hover, focus, and active never on. A rule inside a media
query counts only on a screen where the query holds, so every check is made
at each of the widths the pages are held to, and a media feature the resolver
does not know is refused, never assumed either way. A link's color is not
inherited, because a browser's own stylesheet colors every link directly, so
a link that no rule of the page's stylesheet reaches is drawn in the browser's
blue, or purple once visited. That is what `None` means below, and what these
tests hold the pages clear of.

A link is inline unless a rule lays it out otherwise, and an inline box takes no
minimum height, so the link to the details is as tall as a control only where
the cascade gives it a box of its own and a minimum height of 44 pixels.
"""

import re
from dataclasses import dataclass, field
from html.parser import HTMLParser

import pytest

from blossom.settings import REPOSITORY_ROOT
from tests.support import (
    ESSAY_ID,
    HER_PAGE,
    PAGE_HEADERS,
    browser,
    planned,
    report,
    walkthrough,
)

DETAILS = f"/student/assignments/{ESSAY_ID}"
ACTIONS = f"/student/actions/assignments/{ESSAY_ID}"
STATES = ("link", "visited")
NEVER_ON = {"hover", "focus", "focus-visible", "focus-within", "active"}


# ------------------------------------------------------------- the pages, as elements


@dataclass
class Element:
    tag: str
    classes: frozenset[str]
    attributes: dict[str, str]
    parent: "Element | None"
    text: str = ""

    def ancestors(self) -> list["Element"]:
        found, above = [], self.parent
        while above is not None:
            found.append(above)
            above = above.parent
        return found


class Links(HTMLParser):
    """Every link inside the page's main part, each with the chain of elements above it."""

    VOID = frozenset({"input", "br", "img", "meta", "link", "hr", "source"})

    def __init__(self) -> None:
        super().__init__()
        self.open: list[Element] = []
        self.links: list[Element] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        attributes = {name: value or "" for name, value in attrs}
        element = Element(
            tag=tag,
            classes=frozenset(attributes.get("class", "").split()),
            attributes=attributes,
            parent=self.open[-1] if self.open else None,
        )
        if tag == "a" and any(above.tag == "main" for above in element.ancestors()):
            self.links.append(element)
        if tag not in self.VOID:
            self.open.append(element)

    def handle_endtag(self, tag: str) -> None:
        for index in range(len(self.open) - 1, -1, -1):
            if self.open[index].tag == tag:
                del self.open[index:]
                return

    def handle_data(self, data: str) -> None:
        if self.open and self.open[-1].tag == "a":
            self.open[-1].text += data


def links_in(page: str) -> list[Element]:
    parser = Links()
    parser.feed(page)
    return parser.links


# ------------------------------------------------------------- the stylesheet, as rules


@dataclass(frozen=True)
class Compound:
    tag: str | None = None
    classes: frozenset[str] = frozenset()
    attributes: tuple[tuple[str, str | None], ...] = ()
    pseudo: frozenset[str] = frozenset()
    negated: tuple["Compound", ...] = ()
    known: bool = True

    @property
    def specificity(self) -> tuple[int, int, int]:
        inner = [item.specificity for item in self.negated]
        return (
            0,
            len(self.classes) + len(self.attributes) + len(self.pseudo) + sum(i[1] for i in inner),
            (1 if self.tag else 0) + sum(i[2] for i in inner),
        )

    def matches(self, element: Element, state: str) -> bool:
        if not self.known:
            return False
        if self.tag is not None and self.tag != element.tag:
            return False
        if not self.classes <= element.classes:
            return False
        for name, wanted in self.attributes:
            if name not in element.attributes:
                return False
            if wanted is not None and element.attributes[name] != wanted:
                return False
        for name in self.pseudo:
            if name in NEVER_ON or (name in STATES and name != state):
                return False
        return not any(item.matches(element, state) for item in self.negated)


PIECE = re.compile(
    r"""(?P<not>:not\((?P<inner>[^()]*)\))
      | (?P<class>\.[\w-]+)
      | (?P<attribute>\[(?P<name>[\w-]+)(?:=["']?(?P<value>[^"'\]]*)["']?)?\])
      | (?P<pseudo>::?[\w-]+)
      | (?P<tag>[a-zA-Z][\w-]*|\*)""",
    re.VERBOSE,
)


def compound(text: str) -> Compound:
    tag: str | None = None
    classes: set[str] = set()
    attributes: list[tuple[str, str | None]] = []
    pseudo: set[str] = set()
    negated: list[Compound] = []
    known = True
    position = 0
    while position < len(text):
        piece = PIECE.match(text, position)
        if piece is None:
            return Compound(known=False)
        position = piece.end()
        if piece["not"]:
            negated.append(compound(piece["inner"].strip()))
        elif piece["class"]:
            classes.add(piece["class"][1:])
        elif piece["attribute"]:
            attributes.append((piece["name"], piece["value"]))
        elif piece["pseudo"]:
            name = piece["pseudo"].lstrip(":")
            if piece["pseudo"].startswith("::") or name not in NEVER_ON | set(STATES):
                known = False
            pseudo.add(name)
        elif piece["tag"] and piece["tag"] != "*":
            tag = piece["tag"].lower()
    return Compound(
        tag, frozenset(classes), tuple(attributes), frozenset(pseudo), tuple(negated), known
    )


@dataclass(frozen=True)
class Selector:
    parts: tuple[tuple[str, Compound], ...]
    """Each compound with the combinator that joins it to the one before: a space or ``>``."""

    @property
    def specificity(self) -> tuple[int, int, int]:
        each = [item.specificity for _, item in self.parts]
        return (0, sum(i[1] for i in each), sum(i[2] for i in each))

    def matches(self, element: Element, state: str) -> bool:
        def climb(index: int, at: Element) -> bool:
            joiner, item = self.parts[index]
            if not item.matches(at, state if index == len(self.parts) - 1 else "link"):
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
    for piece in re.findall(r">|[^\s>]+", text.strip()):
        if piece == ">":
            joiner = ">"
            continue
        parts.append((joiner, compound(piece)))
        joiner = " "
    return Selector(tuple(parts))


WIDTHS = (320, 820, 1180, 1440, 3840)
"""The viewport widths, in CSS pixels, the pages are held to: a phone, a tablet upright and
on its side, a laptop, and a large desktop screen."""
ROOT_FONT_PX = 16


class UnreadMedia(AssertionError):
    """A part of the stylesheet this resolver cannot evaluate: a media feature it does not
    know, a nested media query, another at-rule with rules in it, a custom property set
    inside a media query, or a length in a unit it does not read. Raised, and never
    guessed at, since a guess either way could let a link through that some screen
    leaves to the browser's color or draws shorter than a control."""


@dataclass(frozen=True)
class View:
    """One reader's screen: how wide it is, and whether it asks for less motion."""

    width: int
    reduced_motion: bool = False


def length_px(value: str) -> float:
    found = re.fullmatch(r"(\d*\.?\d+)(px|rem|em)", value.strip())
    if found is None:
        raise UnreadMedia(value)
    return float(found.group(1)) * (1 if found.group(2) == "px" else ROOT_FONT_PX)


def holds(condition: str | None, view: View) -> bool:
    """Whether a media condition holds on ``view``. A rule outside any media query holds
    everywhere; a list holds when any of its queries does."""
    if condition is None:
        return True
    return any(one_query_holds(query.strip(), view) for query in condition.split(","))


def one_query_holds(query: str, view: View) -> bool:
    result = True
    for word in re.sub(r"\([^)]*\)", " ", query).split():
        if word == "print":
            result = False
        elif word not in ("and", "only", "screen", "all"):
            raise UnreadMedia(query)
    features = re.findall(r"\(\s*([\w-]+)\s*:\s*([^)]+?)\s*\)", query)
    if len(features) != query.count("("):
        raise UnreadMedia(query)
    for name, value in features:
        if name == "min-width":
            result = result and view.width >= length_px(value)
        elif name == "max-width":
            result = result and view.width <= length_px(value)
        elif name == "prefers-reduced-motion":
            if value not in ("reduce", "no-preference"):
                raise UnreadMedia(query)
            result = result and (value == "reduce") == view.reduced_motion
        else:
            raise UnreadMedia(query)
    return result


@dataclass
class Sheet:
    tokens: dict[str, str] = field(default_factory=dict)
    colors: list[tuple[Selector, str, int, str | None]] = field(default_factory=list)
    """Each rule that sets a color: its selector, the value, its place in the source, and
    the media condition it sits under, ``None`` for a rule that holds everywhere."""
    backgrounds: list[tuple[Selector, str, int, str | None]] = field(default_factory=list)
    """The same for each rule that sets a background, by either property's name."""
    displays: list[tuple[Selector, str, int, str | None]] = field(default_factory=list)
    """The same for each rule that sets how an element is laid out."""
    heights: list[tuple[Selector, str, int, str | None]] = field(default_factory=list)
    """The same for each rule that sets a minimum height."""


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


def read_sheet(css: str) -> Sheet:
    """Every rule that sets ``color``, a background, ``display``, or ``min-height``, in source
    order, each with the media condition it sits under, and the sheet's custom
    properties. A rule inside a media query applies only where the query holds, so a
    link whose only color comes from one is still the browser's color on every other
    screen; ``color_of`` is asked about one screen at a time. Font faces and keyframes
    hold no rules for elements and are passed over."""
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
                    msg = f"a media query inside {media}"
                    raise UnreadMedia(msg)
                read(inside, before.removeprefix("@media").strip())
                continue
            if before.startswith("@"):
                raise UnreadMedia(before)
            order += 1
            declared = {
                name.strip(): value.strip()
                for name, _, value in (line.partition(":") for line in inside.split(";"))
                if value
            }
            for name, value in declared.items():
                if name.startswith("--"):
                    if media is not None:
                        msg = f"--{name[2:]} is set inside {media}"
                        raise UnreadMedia(msg)
                    sheet.tokens[name[2:]] = value
            for head in before.split(","):
                if "color" in declared:
                    sheet.colors.append((selector(head), declared["color"], order, media))
                for name in ("background", "background-color"):
                    if name in declared:
                        sheet.backgrounds.append((selector(head), declared[name], order, media))
                if "display" in declared:
                    sheet.displays.append((selector(head), declared["display"], order, media))
                if "min-height" in declared:
                    sheet.heights.append((selector(head), declared["min-height"], order, media))

    read(plain, None)
    return sheet


Rules = list[tuple[Selector, str, int, str | None]]
Paint = tuple[float, float, float, float]
PLAIN_SURFACES = ("canvas", "surface", "surface-strong")
"""The paper and the two card surfaces: what a link sits on when it is on no tint."""


def winning(rules: Rules, sheet: Sheet, element: Element, state: str, view: View) -> str | None:
    """The value the cascade settles on for ``element`` among ``rules``, its custom property
    resolved, or ``None`` when no rule that holds on ``view`` reaches it."""
    winner: tuple[tuple[int, int, int], int, str] | None = None
    for chosen, value, order, media in rules:
        if holds(media, view) and chosen.matches(element, state):
            rank = (chosen.specificity, order, value)
            if winner is None or rank[:2] > winner[:2]:
                winner = rank
    if winner is None:
        return None
    named = re.fullmatch(r"var\(--([\w-]+)\)", winner[2])
    return sheet.tokens[named.group(1)] if named else winner[2]


def color_of(sheet: Sheet, element: Element, state: str, view: View) -> str | None:
    """The color the sheet gives ``element`` in ``state`` on ``view``, or ``None`` when no
    rule that holds there reaches it and the browser's own color would show."""
    return winning(sheet.colors, sheet, element, state, view)


def paint(value: str | None) -> Paint | None:
    """A flat color as red, green, blue, and how opaque it is, or ``None`` for a value that
    paints nothing flat: none at all, transparent, the text's own color, or a gradient."""
    if value is None:
        return None
    if re.fullmatch(r"#[0-9a-fA-F]{6}", value):
        red, green, blue = (int(value[i : i + 2], 16) for i in (1, 3, 5))
        return (red, green, blue, 1.0)
    found = re.fullmatch(r"rgba?\(([^)]*)\)", value)
    if found is None:
        return None
    parts = [float(part) for part in re.findall(r"[\d.]+", found.group(1))]
    return (parts[0], parts[1], parts[2], parts[3] if len(parts) > 3 else 1.0)


def over(top: Paint, under: tuple[float, float, float]) -> tuple[float, float, float]:
    alpha = top[3]
    return (
        top[0] * alpha + under[0] * (1 - alpha),
        top[1] * alpha + under[1] * (1 - alpha),
        top[2] * alpha + under[2] * (1 - alpha),
    )


def tint_under(sheet: Sheet, link: Element, view: View) -> Paint | None:
    """The tint ``link`` sits on, as the stylesheet paints it: the background of the nearest
    element above it that has a flat one, unless that is the paper or a card, which is
    no tint. Found from the stylesheet's own rules, so a panel tinted tomorrow is held to
    the same contrast with no word added here."""
    plain = {paint(sheet.tokens[name]) for name in PLAIN_SURFACES}
    for above in link.ancestors():
        painted = paint(winning(sheet.backgrounds, sheet, above, "link", view))
        if painted is not None:
            return None if painted in plain else painted
    return None


def readable_on(sheet: Sheet, ink: str, tint: Paint) -> float:
    """The least contrast ``ink`` has against ``tint`` wherever a tinted panel is laid: over
    the paper, and over each card surface on the paper."""
    written = paint(ink)
    assert written is not None, ink
    return min(
        contrast(written[:3], over(tint, drawn(sheet, *under)))
        for under in (("canvas",), ("canvas", "surface-strong"), ("canvas", "surface"))
    )


def stylesheet() -> str:
    return (REPOSITORY_ROOT / "blossom" / "static" / "blossom.css").read_text(encoding="utf-8")


RENDERED: dict[str, str] = {}


@pytest.fixture
def rendered() -> dict[str, str]:
    """Her week with a notice above a marked plan, the family page, an assignment's details
    after a save, and a refused save: the pages that hold every kind of link in question.

    Rendered by the first test that asks and kept for the rest, since nothing here changes
    them. The fixture itself is a test's own, never the module's: the suite gives each
    test a temporary state folder through a fixture of that scope, and one of a wider
    scope would start the application before it, on the state folder inside the checkout.
    """
    if RENDERED:
        return RENDERED
    with browser(key=True) as client:
        walkthrough(client)
        planned(client)
        report(client, ESSAY_ID, "done")
        after = client.get(f"{DETAILS}?said=saved&return_to=today", headers=PAGE_HEADERS).text
        refused = client.post(
            f"{ACTIONS}/report",
            data={
                "note": "",
                "expected_report_id": "",
                "week": "",
                "report_view": "detail",
                "return_to": "today",
                "plan_id": "",
            },
            headers=PAGE_HEADERS,
        )
        assert refused.status_code == 422
        RENDERED.update(
            {
                "her week": client.get(HER_PAGE, headers=PAGE_HEADERS).text,
                "family": client.get("/parent", headers=PAGE_HEADERS).text,
                "details": after,
                "refused": refused.text,
            }
        )
    return RENDERED


# ------------------------------------------------------------- the tests


def test_the_resolver_reads_the_cascade_the_way_a_browser_would() -> None:
    """The parts of CSS it claims: a class beats a type, a later rule beats an earlier one of
    the same weight, `:visited` applies to a visited link alone, hover never applies,
    `:not([class])` leaves a classed link out, a child combinator wants a parent, and a
    link nothing reaches has no color of the page's. A rule inside a media query colors a
    link only where the query holds: a link whose one color is for narrow screens is the
    browser's color on a wide one, a rule for wide screens wins there and nowhere else,
    and a query for less motion follows the reader's setting. What it cannot evaluate, it
    refuses."""
    sheet = read_sheet(
        """
        :root { --ink: #111111; --soft: #222222; }
        main a { color: var(--ink); }
        main a { color: var(--soft); }
        a.named { color: #333333; }
        a.named:visited { color: #444444; }
        a.named:hover { color: #555555; }
        .panel a:not([class]) { color: #666666; }
        .row > a { color: #777777; }
        @media (max-width: 30rem) { footer a { color: #888888; } }
        @media screen and (min-width: 72rem) { main .row > a { color: #999999; } }
        @media (prefers-reduced-motion: reduce) { a.named { color: #aaaaaa; } }
        @media print { main a { color: #000000; } }
        """
    )
    (plain, named, tinted, classed_on_tint, child, grandchild) = links_in(
        """<main><p><a href="/">x</a> <a class="named" href="/">y</a></p>
        <div class="panel"><p><a href="/">z</a> <a class="named" href="/">w</a></p></div>
        <div class="row"><a href="/">c</a><p><a href="/">g</a></p></div></main>
        <footer><a href="/">f</a></footer>"""
    )
    outside = Element("a", frozenset(), {}, Element("footer", frozenset(), {}, None))
    phone, tablet, desktop = View(320), View(820), View(1440)

    assert [color_of(sheet, plain, state, tablet) for state in STATES] == ["#222222", "#222222"]
    assert [color_of(sheet, named, state, tablet) for state in STATES] == ["#333333", "#444444"]
    assert color_of(sheet, tinted, "link", tablet) == "#666666"
    assert color_of(sheet, classed_on_tint, "link", tablet) == "#333333"
    assert color_of(sheet, child, "link", tablet) == "#777777"
    assert color_of(sheet, grandchild, "link", tablet) == "#222222"
    assert color_of(read_sheet("p a:hover { color: red; }"), plain, "link", tablet) is None
    assert color_of(sheet, outside, "visited", phone) == "#888888"
    assert color_of(sheet, outside, "visited", View(480)) == "#888888"
    assert color_of(sheet, outside, "visited", View(481)) is None
    assert color_of(sheet, outside, "visited", desktop) is None
    assert color_of(sheet, child, "link", View(1151)) == "#777777"
    assert color_of(sheet, child, "link", View(1152)) == "#999999"
    assert color_of(sheet, named, "link", View(820, reduced_motion=True)) == "#aaaaaa"
    still = read_sheet(
        "@media (prefers-reduced-motion: no-preference) { main a { color: #bbbbbb; } }"
    )
    assert color_of(still, plain, "link", tablet) == "#bbbbbb"
    assert color_of(still, plain, "link", View(820, reduced_motion=True)) is None
    assert color_of(sheet, plain, "link", desktop) == "#222222"
    for unread in (
        "@media (hover: hover) { a { color: red; } }",
        "@media (min-width: 40vw) { a { color: red; } }",
        "@media not screen { a { color: red; } }",
        "@supports (display: grid) { a { color: red; } }",
        "@media (max-width: 30rem) { :root { --ink: red; } }",
        "@media (prefers-reduced-motion: reduced) { a { color: red; } }",
        "@media (prefers-reduced-motion) { a { color: red; } }",
    ):
        with pytest.raises(UnreadMedia):
            color_of(read_sheet(unread), plain, "link", tablet)


@pytest.mark.parametrize("width", WIDTHS)
@pytest.mark.parametrize("state", STATES)
def test_every_link_in_a_page_gets_its_color_from_the_stylesheet(
    rendered: dict[str, str], state: str, width: int
) -> None:
    """On her week, the family page, an assignment's details, and a refused save, at each
    width the pages are held to and whether or not the reader asks for less motion, no
    link in the page's main part is left to the browser's blue or purple, visited or not.
    Every link that sits on a tint, whatever its class, has at least 4.5 to 1 against that
    tint, over the paper and over a card; which panels are tinted is read from the
    stylesheet's backgrounds. By name: on a tint, the plain links and the links to
    assignments alike take the darker action color, in a notice and in a plan row she
    reports done; on a plain surface the way back beside a saved update and a link to an
    assignment take the action color."""
    sheet = read_sheet(stylesheet())
    action, darker = sheet.tokens["blue-action"], sheet.tokens["blue-action-hover"]
    for view in (View(width), View(width, reduced_motion=True)):
        seen: dict[tuple[str, str, str], str] = {}
        for name, page in rendered.items():
            found = links_in(page)
            assert found, name
            for link in found:
                color = color_of(sheet, link, state, view)
                where = (name, link.text.strip(), sorted(link.classes), view)
                assert color is not None, where
                tint = tint_under(sheet, link, view)
                if tint is not None:
                    assert readable_on(sheet, color, tint) >= 4.5, where
                placed = "tint" if tint is not None else "plain"
                inside = next(
                    (
                        mark
                        for above in link.ancestors()
                        for mark in ("reported-done", "confidence", "problem", "update-result")
                        if mark in above.classes and above.tag != "details"
                    ),
                    "",
                )
                key = (placed, inside, link.text.strip())
                assert seen.setdefault(key, color) == color, where
        essay, proposal = "Canal Era comparison essay", "Science fair topic proposal"
        assert seen["plain", "update-result", "Back to today's plan"] == action
        assert seen["tint", "confidence", "What the sources say is below."] == darker
        assert seen["tint", "problem", "Go to the update."] == darker
        assert seen["tint", "confidence", "View today's plan"] == darker
        assert seen["tint", "confidence", essay] == darker
        assert seen["tint", "reported-done", essay] == darker
        assert seen["plain", "", proposal] == action


PLAIN_RULE = "main a {\n  color: var(--blue-action);\n}"
ONLY_ON_A_PHONE = "@media (max-width: 30rem) {\n  main a {\n    color: var(--blue-action);\n  }\n}"


@pytest.mark.parametrize(
    ("was", "becomes", "words", "inside", "colored_up_to", "falls_to"),
    [
        ("main a {", "main b {", "Back to today's plan", "update-result", 0, None),
        (
            "main .problem a:not([class]),",
            "main .problem b:not([class]),",
            "Go to the update.",
            "problem",
            0,
            "blue-action",
        ),
        (PLAIN_RULE, ONLY_ON_A_PHONE, "Back to today's plan", "update-result", 480, None),
        (
            "main .confidence a.assignment-link,",
            "main .confidence b.assignment-link,",
            "Canal Era comparison essay",
            "confidence",
            0,
            "blue-action",
        ),
        (
            "main .plan-rows .reported-done a.assignment-link {",
            "main .plan-rows .reported-done b.assignment-link {",
            "Canal Era comparison essay",
            "plan-block",
            0,
            "blue-action",
        ),
    ],
)
def test_the_check_fails_when_a_rule_stops_reaching_its_links(
    rendered: dict[str, str],
    was: str,
    becomes: str,
    words: str,
    inside: str,
    colored_up_to: int,
    falls_to: str | None,
) -> None:
    """The same pages against a stylesheet whose rule is still there in words and does not
    do its work. With the plain rule's selector broken, the way back beside a saved update
    has no color of the page's at any width, visited or not, which is the browser's blue
    and purple. With a tint rule broken, for a problem's plain link, for a link to an
    assignment in a notice, or for one in a plan row she reports done, the link falls back
    to the action color that is too light for its tint.
    With the plain rule moved inside a query for narrow
    screens, the link is colored on a phone and left to the browser on every wider
    screen. Each time the check above would fail, so it is the cascade on each screen it
    holds, and not the words of the stylesheet."""
    css = stylesheet()
    assert css.count(was) == 1
    sheet = read_sheet(css.replace(was, becomes))
    action = sheet.tokens["blue-action"]
    found = [
        link
        for page in rendered.values()
        for link in links_in(page)
        if link.text.strip() == words
        and any(inside in above.classes for above in link.ancestors())
        and (inside != "plan-block" or tint_under(sheet, link, View(820)) is not None)
    ]

    assert found
    for link in found:
        for state in STATES:
            for width in WIDTHS:
                expected = (
                    action
                    if width <= colored_up_to
                    else (None if falls_to is None else sheet.tokens[falls_to])
                )
                assert color_of(sheet, link, state, View(width)) == expected, (words, width)


def drawn(sheet: Sheet, *layers: str) -> tuple[float, float, float]:
    """The color a reader sees where the sheet's tokens are laid one over another, the last
    on top, starting from an opaque one."""

    def token(name: str) -> Paint:
        painted = paint(sheet.tokens[name])
        assert painted is not None, name
        return painted

    under: tuple[float, float, float] = token(layers[0])[:3]
    for name in layers[1:]:
        under = over(token(name), under)
    return under


def contrast(one: tuple[float, float, float], other: tuple[float, float, float]) -> float:
    """The contrast ratio of two colors as the accessibility guidelines measure it."""

    def luminance(color: tuple[float, float, float]) -> float:
        parts = [
            part / 255 / 12.92 if part / 255 <= 0.03928 else ((part / 255 + 0.055) / 1.055) ** 2.4
            for part in color
        ]
        return 0.2126 * parts[0] + 0.7152 * parts[1] + 0.0722 * parts[2]

    high, low = sorted((luminance(one), luminance(other)), reverse=True)
    return (high + 0.05) / (low + 0.05)


def test_the_darker_action_color_clears_every_tint_and_the_usual_one_does_not() -> None:
    """Why a plain link on a tint takes the darker color: against each tint, over the paper
    and over a card, the darker action color is at least 4.5 to 1 and the usual one is
    under it; on the paper and on a card alone the usual one clears it."""
    sheet = read_sheet(stylesheet())
    darker, usual = drawn(sheet, "blue-action-hover"), drawn(sheet, "blue-action")

    for tint in ("rose-bg", "amber-bg", "slate-bg", "sage-bg"):
        for under in (("canvas",), ("canvas", "surface-strong"), ("canvas", "surface")):
            panel = drawn(sheet, *under, tint)
            assert contrast(darker, panel) >= 4.5, (tint, under)
            assert contrast(usual, panel) < 4.5, (tint, under)
    for under in (("canvas",), ("canvas", "surface-strong")):
        assert contrast(usual, drawn(sheet, *under)) >= 4.5, under


# ------------------------------------------------------------- as tall as a control


ALGEBRA_ID = "assignment-algebra-set"
LOG_ID = "assignment-reading-log"
TOUCH_HEIGHT_PX = 44
"""The least height of a control on the pages, a button's or a link's: 2.75rem at the root size."""
BOXED = frozenset({"block", "inline-block", "flex", "inline-flex", "grid", "inline-grid"})
"""The layouts that give a link a box of its own, which a minimum height applies to."""
PLACES = frozenset(
    {
        "a card",
        "a card she reports done",
        "a row due later",
        "a row due later she reports done",
        "the card shown apart",
    }
)
"""Every place on her week that holds the link to an assignment's details."""
CARDS = PLACES - {"a row due later", "a row due later she reports done"}
ROWS = PLACES - CARDS
DETAILS_RULE = (
    ".details-link a,\na.details-link {\n  display: inline-flex;\n  align-items: center;\n"
    "  min-height: 2.75rem;\n  margin: 0;\n}"
)
HER_WEEKS: dict[str, str] = {}


@pytest.fixture
def her_weeks() -> dict[str, str]:
    """Her week with the essay and the algebra set reported done, and an earlier week whose
    address names the reading log, which is outside it. Kept once rendered, as ``rendered`` is."""
    if HER_WEEKS:
        return HER_WEEKS
    with browser() as client:
        report(client, ESSAY_ID, "done")
        report(client, ALGEBRA_ID, "done")
        HER_WEEKS.update(
            {
                "her week": client.get(HER_PAGE, headers=PAGE_HEADERS).text,
                "an earlier week": client.get(
                    HER_PAGE, params={"week": "2026-08-10", "show": LOG_ID}, headers=PAGE_HEADERS
                ).text,
            }
        )
    return HER_WEEKS


def details_links(pages: dict[str, str]) -> list[Element]:
    return [
        link for page in pages.values() for link in links_in(page) if link.text.strip() == "Details"
    ]


def place_of(link: Element) -> str:
    """Where a link stands on her week, read from the elements above it."""
    above = link.ancestors()
    if any("apart" in item.classes for item in above):
        return "the card shown apart"
    group = "a row due later" if any("assigned" in item.classes for item in above) else "a card"
    folded = any(item.tag == "details" and "reported-done" in item.classes for item in above)
    return f"{group} she reports done" if folded else group


def height_px(value: str) -> float:
    """A minimum height in CSS pixels. An em follows the element's own font size, which is not
    worked out here, so only pixels and root ems are read."""
    found = re.fullmatch(r"(\d*\.?\d+)(px|rem)", value.strip())
    if found is None:
        raise UnreadMedia(value)
    return float(found.group(1)) * (1 if found.group(2) == "px" else ROOT_FONT_PX)


def short_of_a_control(sheet: Sheet, link: Element, view: View) -> bool:
    """Whether ``link`` on ``view`` is left inline, or given a box with no minimum height or a
    minimum under a control's."""
    display = winning(sheet.displays, sheet, link, "link", view)
    least = winning(sheet.heights, sheet, link, "link", view)
    return display not in BOXED or least is None or height_px(least) < TOUCH_HEIGHT_PX


@pytest.mark.parametrize("width", WIDTHS)
def test_every_details_link_is_as_tall_as_a_control(her_weeks: dict[str, str], width: int) -> None:
    """In every place on her week, at each width, with or without less motion: 44 pixels."""
    sheet = read_sheet(stylesheet())
    found = details_links(her_weeks)

    assert {place_of(link) for link in found} == PLACES
    for view in (View(width), View(width, reduced_motion=True)):
        for link in found:
            where = (place_of(link), link.attributes["aria-label"], view)
            assert not short_of_a_control(sheet, link, view), where


@pytest.mark.parametrize(
    ("becomes", "short", "fine_up_to"),
    [
        pytest.param(
            DETAILS_RULE.replace(".details-link a,", ".details-link b,"),
            CARDS,
            0,
            id="the-cards-selector-broken",
        ),
        pytest.param(
            DETAILS_RULE.replace("a.details-link {", "b.details-link {"),
            ROWS,
            0,
            id="the-rows-selector-broken",
        ),
        pytest.param(
            DETAILS_RULE.replace("inline-flex", "inline"), PLACES, 0, id="laid-out-inline"
        ),
        pytest.param(DETAILS_RULE.replace("2.75rem", "1.5rem"), PLACES, 0, id="too-short"),
        pytest.param(
            f"@media (max-width: 30rem) {{\n{DETAILS_RULE}\n}}", PLACES, 480, id="only-on-a-phone"
        ),
    ],
)
def test_the_height_check_fails_when_the_details_rule_stops_doing_its_work(
    her_weeks: dict[str, str], becomes: str, short: frozenset[str], fine_up_to: int
) -> None:
    """Each link the broken rule leaves is short of a control, on every screen it leaves it."""
    css = stylesheet()
    assert css.count(DETAILS_RULE) == 1
    sheet = read_sheet(css.replace(DETAILS_RULE, becomes))
    found = details_links(her_weeks)

    assert {place_of(link) for link in found} == PLACES
    for link in found:
        for width in WIDTHS:
            expected = place_of(link) in short and width > fine_up_to
            assert short_of_a_control(sheet, link, View(width)) == expected, (place_of(link), width)
