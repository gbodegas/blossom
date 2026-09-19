"""The color a link ends up with, worked out from the stylesheet and the pages as rendered.

No browser runs in these tests, so the cascade is resolved here, for the part
of CSS the stylesheet uses on links: compound selectors of a type, classes,
attributes, and pseudo-classes, with descendant and child combinators, in
comma lists; specificity, then source order; `:visited` and `:link` by the
state asked for, and hover, focus, and active never on. A link's color is not
inherited, because a browser's own stylesheet colors every link directly, so
a link that no rule of the page's stylesheet reaches is drawn in the browser's
blue, or purple once visited. That is what `None` means below, and what these
tests hold the pages clear of.
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
TINTED = ("problem", "confidence", "from-parents")
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


@dataclass
class Sheet:
    tokens: dict[str, str] = field(default_factory=dict)
    colors: list[tuple[Selector, str, int]] = field(default_factory=list)


def read_sheet(css: str) -> Sheet:
    """Every rule that sets ``color``, in source order, with the sheet's custom properties.
    Rules inside a media query are read as applying, which can only make the test stricter
    about a link that some width leaves uncolored; keyframes and font faces hold none."""
    sheet = Sheet()
    plain = re.sub(r"/\*.*?\*/", "", css, flags=re.S)
    plain = re.sub(r"@(?:font-face|keyframes)[^{]*\{(?:[^{}]|\{[^{}]*\})*\}", "", plain)
    plain = re.sub(r"@media[^{]*\{((?:[^{}]|\{[^{}]*\})*)\}", r"\1", plain)
    for order, (heads, body) in enumerate(re.findall(r"([^{}]+)\{([^{}]*)\}", plain)):
        declared = {
            name.strip(): value.strip()
            for name, _, value in (line.partition(":") for line in body.split(";"))
            if value
        }
        for name, value in declared.items():
            if name.startswith("--"):
                sheet.tokens[name[2:]] = value
        if "color" in declared:
            for head in heads.split(","):
                sheet.colors.append((selector(head), declared["color"], order))
    return sheet


def color_of(sheet: Sheet, element: Element, state: str) -> str | None:
    """The color the sheet gives ``element`` in ``state``, its custom property resolved, or
    ``None`` when no rule reaches it and the browser's own color would show."""
    winner: tuple[tuple[int, int, int], int, str] | None = None
    for chosen, value, order in sheet.colors:
        if chosen.matches(element, state):
            rank = (chosen.specificity, order, value)
            if winner is None or rank[:2] > winner[:2]:
                winner = rank
    if winner is None:
        return None
    named = re.fullmatch(r"var\(--([\w-]+)\)", winner[2])
    return sheet.tokens[named.group(1)] if named else winner[2]


def on_a_tint(element: Element) -> bool:
    return any(above.classes & set(TINTED) for above in element.ancestors())


def stylesheet() -> str:
    return (REPOSITORY_ROOT / "blossom" / "static" / "blossom.css").read_text(encoding="utf-8")


def pages() -> dict[str, str]:
    """Her week with a notice above a marked plan, the family page, an assignment's details
    after a save, and a refused save: the pages that hold every kind of link in question."""
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
        return {
            "her week": client.get(HER_PAGE, headers=PAGE_HEADERS).text,
            "family": client.get("/parent", headers=PAGE_HEADERS).text,
            "details": after,
            "refused": refused.text,
        }


# ------------------------------------------------------------- the tests


def test_the_resolver_reads_the_cascade_the_way_a_browser_would() -> None:
    """The parts of CSS it claims: a class beats a type, a later rule beats an earlier one of
    the same weight, `:visited` applies to a visited link alone, hover never applies,
    `:not([class])` leaves a classed link out, a child combinator wants a parent, and a
    link nothing reaches has no color of the page's."""
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
        """
    )
    (plain, named, tinted, classed_on_tint, child, grandchild) = links_in(
        """<main><p><a href="/">x</a> <a class="named" href="/">y</a></p>
        <div class="panel"><p><a href="/">z</a> <a class="named" href="/">w</a></p></div>
        <div class="row"><a href="/">c</a><p><a href="/">g</a></p></div></main>
        <footer><a href="/">f</a></footer>"""
    )
    outside = Element("a", frozenset(), {}, Element("footer", frozenset(), {}, None))

    assert [color_of(sheet, plain, state) for state in STATES] == ["#222222", "#222222"]
    assert [color_of(sheet, named, state) for state in STATES] == ["#333333", "#444444"]
    assert color_of(sheet, tinted, "link") == "#666666"
    assert color_of(sheet, classed_on_tint, "link") == "#333333"
    assert color_of(sheet, child, "link") == "#777777"
    assert color_of(sheet, grandchild, "link") == "#222222"
    assert color_of(sheet, outside, "visited") == "#888888"
    assert color_of(read_sheet("p a:hover { color: red; }"), plain, "link") is None


@pytest.mark.parametrize("state", STATES)
def test_every_link_in_a_page_gets_its_color_from_the_stylesheet(state: str) -> None:
    """On her week, the family page, an assignment's details, and a refused save, no link
    in the page's main part is left to the browser's blue or purple, visited or not. A link
    with no class of its own takes the darker action color on a tint, every one of them;
    the way back beside a saved update takes the action color, from the plain rule; and a
    link to an assignment keeps the action color its own rule gives it, which wins over
    the plain rule either way."""
    sheet = read_sheet(stylesheet())
    action, darker = sheet.tokens["blue-action"], sheet.tokens["blue-action-hover"]
    seen: dict[str, str] = {}
    for name, page in pages().items():
        found = links_in(page)
        assert found, name
        for link in found:
            color = color_of(sheet, link, state)
            where = (name, link.text.strip(), sorted(link.classes))
            assert color is not None, where
            if not link.classes:
                kind = "tint" if on_a_tint(link) else "plain"
                seen[f"{kind}: {link.text.strip()}"] = color
                if kind == "tint":
                    assert color == darker, where
            elif "assignment-link" in link.classes:
                assert color == action, where
    assert seen["plain: Back to today's plan"] == action
    assert seen["tint: What the sources say is below."] == darker
    assert seen["tint: Go to the update."] == darker
    assert seen["tint: View today's plan"] == darker


@pytest.mark.parametrize(
    ("broken", "words", "inside", "falls_to"),
    [
        ("main a {", "Back to today's plan", "update-result", None),
        ("main .problem a:not([class]),", "Go to the update.", "problem", "blue-action"),
    ],
)
def test_the_check_fails_when_a_rule_stops_reaching_its_links(
    broken: str, words: str, inside: str, falls_to: str | None
) -> None:
    """The same pages against a stylesheet whose rule is still there in words and reaches
    nothing. With the plain rule broken, the way back beside a saved update has no color of
    the page's, visited or not, which is the browser's blue and purple. With the tint rule
    broken, a problem's link falls back to the action color that is too light for its
    tint. Either way the check above would fail, so it is the cascade it holds, and not
    the words of the stylesheet."""
    css = stylesheet()
    assert css.count(broken) == 1
    sheet = read_sheet(css.replace(broken, broken.replace(" a", " b")))
    found = [
        link
        for page in pages().values()
        for link in links_in(page)
        if link.text.strip() == words and any(inside in above.classes for above in link.ancestors())
    ]

    assert found
    for link in found:
        for state in STATES:
            expected = None if falls_to is None else sheet.tokens[falls_to]
            assert color_of(sheet, link, state) == expected, (words, state)


def drawn(sheet: Sheet, *layers: str) -> tuple[float, float, float]:
    """The color a reader sees where the sheet's tokens are laid one over another, the last
    on top, starting from an opaque one."""

    def token(name: str) -> tuple[float, float, float, float]:
        value = sheet.tokens[name]
        if value.startswith("#"):
            red, green, blue = (int(value[i : i + 2], 16) for i in (1, 3, 5))
            return (red, green, blue, 1.0)
        parts = [float(part) for part in re.findall(r"[\d.]+", value)]
        return (parts[0], parts[1], parts[2], parts[3] if len(parts) > 3 else 1.0)

    under: tuple[float, float, float] = token(layers[0])[:3]
    for name in layers[1:]:
        red, green, blue, alpha = token(name)
        under = (
            red * alpha + under[0] * (1 - alpha),
            green * alpha + under[1] * (1 - alpha),
            blue * alpha + under[2] * (1 - alpha),
        )
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
