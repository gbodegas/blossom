"""The color a link ends up with, and the height of the links held to a control's, worked
out from the stylesheet and the pages as rendered.

No browser runs in these tests, so the cascade is resolved here, on the page
and stylesheet reading in `tests/support.py`, for the part of CSS the
stylesheet uses on links: compound selectors of a type, an id, classes,
attributes, and pseudo-classes, with descendant and child combinators, in
comma lists; specificity, then source order; `:visited` and `:link` on a link
by the state asked for, and hover, focus, and active never on. A rule inside a media
query counts only on a screen where the query holds, so every check is made
at each of the widths the pages are held to, and a media feature the resolver
does not know is refused, never assumed either way. A link's color is not
inherited, because a browser's own stylesheet colors every link directly, so
a link that no rule of the page's stylesheet reaches is drawn in the browser's
blue, or purple once visited. That is what `None` means below, and what these
tests hold the pages clear of.

A link is inline unless a rule lays it out otherwise, and an inline box takes no
minimum height, so a link to the details is as tall as a control only where
the cascade gives it a box of its own and a minimum height of 44 pixels. The
one that ends the hand-in line's sentence takes a box that stays in the line,
and so do the titles that lead the rows of her To turn in list and of the
family page's updates, the family page's link to adding assignments, the line
of links under the homework notes, and the link that ends each homework note's
row. Each link in her week's line of school reports to check stays in the words
of its sentence as an inline box, padded out to 44 pixels, inside an item of the
line padded as far, so the line grows around each item and a sentence that
wraps never lays one link's area over another's, with a reader's own spacing
too.
"""

import pathlib
import re
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from html import unescape

import pytest

from blossom.hand_in import NEEDS_HAND_IN, HandInSaved
from blossom.reconciliation import SourceChannel
from blossom.routes.navigation import NEW_NOTE_PAGE, NOTE_ACTIONS, NOTES_PAGE, TO_TURN_IN_PAGE
from blossom.settings import REPOSITORY_ROOT
from blossom.stores.project_state import ProjectStateStore
from tests.support import (
    DETAILS,
    ESSAY_ID,
    HER_PAGE,
    PAGE_HEADERS,
    PLAN_DATE,
    QUIZ_ID,
    Element,
    Rule,
    UnreadCss,
    View,
    browser,
    due,
    elements_of,
    form_fields,
    hidden,
    household_client,
    planned,
    report,
    reported,
    rules_named,
    school_said,
    selector,
    sign_in_as,
    state_of,
    style_rules,
    unshielded,
    walkthrough,
    winner,
)
from tests.support import READING_LOG_ID as LOG_ID

ACTIONS = f"/student/actions/assignments/{ESSAY_ID}"
STATES = ("link", "visited")


# ------------------------------------------------------------- the pages, as elements


def links_in(page: str) -> list[Element]:
    """Every link inside the page's main part, each with the chain of elements above it."""
    return [
        one
        for one in elements_of(page)
        if one.tag == "a" and any(above.tag == "main" for above in one.ancestors())
    ]


# ------------------------------------------------------------- the stylesheet, as rules


WIDTHS = (320, 820, 1180, 1440, 3840)
"""The viewport widths, in CSS pixels, the pages are held to: a phone, a tablet upright and
on its side, a laptop, and a large desktop screen."""
ROOT_FONT_PX = 16


@dataclass
class Sheet:
    tokens: dict[str, str] = field(default_factory=dict)
    colors: list[Rule] = field(default_factory=list)
    """Each rule that sets a color: its selector, the value, its place in the source, and
    the media condition it sits under, ``None`` for a rule that holds everywhere."""
    backgrounds: list[Rule] = field(default_factory=list)
    """The same for each rule that sets a background, by either property's name."""
    displays: list[Rule] = field(default_factory=list)
    """The same for each rule that sets how an element is laid out."""
    heights: list[Rule] = field(default_factory=list)
    """The same for each rule that sets a minimum height."""
    paddings: list[Rule] = field(default_factory=list)
    """The same for each rule that sets a padding, by its shorthand, as the sheet writes it."""


def read_sheet(css: str) -> Sheet:
    """Every rule that sets ``color``, a background, ``display``, ``min-height``, or
    ``padding``, in source order, each with the media condition it sits under, and the
    sheet's custom properties. A rule inside a media query applies only where the query
    holds, so a link whose only color comes from one is still the browser's color on every
    other screen; ``color_of`` is asked about one screen at a time. A custom property takes
    its last important value over every normal one, whichever rule holds each, as the
    cascade weighs them."""
    sheet = Sheet()
    weighty: set[str] = set()
    for style in style_rules(css):
        declared: dict[str, tuple[str, bool]] = {}
        for name, value, important in style.declarations:
            if important or not declared.get(name, ("", False))[1]:
                declared[name] = (value.lower() if name == "display" else value, important)
        for name, (value, important) in declared.items():
            if name.startswith("--"):
                if style.media is not None:
                    msg = f"--{name[2:]} is set inside {style.media}"
                    raise UnreadCss(msg)
                if important or name not in weighty:
                    sheet.tokens[name[2:]] = value
                if important:
                    weighty.add(name)
        lists = {
            "color": sheet.colors,
            "background": sheet.backgrounds,
            "background-color": sheet.backgrounds,
            "display": sheet.displays,
            "min-height": sheet.heights,
            "padding": sheet.paddings,
        }
        for head in style.selectors.split(","):
            for name, (value, important) in declared.items():
                if name in lists:
                    chosen = selector(unshielded(head))
                    rule = Rule(chosen, value, style.order, style.media, important)
                    lists[name].append(rule)
    return sheet


Paint = tuple[float, float, float, float]
PLAIN_SURFACES = ("canvas", "surface", "surface-strong")
"""The paper and the two card surfaces: what a link sits on when it is on no tint."""


def winning(
    rules: list[Rule], sheet: Sheet, element: Element, state: str, view: View
) -> str | None:
    """The value the cascade settles on for ``element`` among ``rules``, its custom property
    resolved, or ``None`` when no rule that holds on ``view`` reaches it."""
    found = winner([rule for rule in rules if rule.chosen.matches(element, state)], view)
    if found is None:
        return None
    named = re.fullmatch(r"var\(--([\w-]+)\)", found.value)
    return sheet.tokens[named.group(1)] if named else found.value


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
    and a query for less motion follows the reader's setting. A query is read in any case.
    What it cannot evaluate, it refuses."""
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
    outside = Element("a", {}, Element("footer", {}, None))
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
    shouted = read_sheet("@MEDIA SCREEN AND (MAX-WIDTH: 30REM) { footer a { color: #888888; } }")
    assert color_of(shouted, outside, "visited", View(480)) == "#888888"
    assert color_of(shouted, outside, "visited", View(481)) is None
    assert color_of(sheet, plain, "link", desktop) == "#222222"
    for unread in (
        "@media (hover: hover) { a { color: red; } }",
        "@media (min-width: 40vw) { a { color: red; } }",
        "@media not screen { a { color: red; } }",
        "@supports (display: grid) { a { color: red; } }",
        "@media (max-width: 30rem) { :root { --ink: red; } }",
        "@media (prefers-reduced-motion: reduced) { a { color: red; } }",
        "@media (prefers-reduced-motion) { a { color: red; } }",
        "@MEDIA (PREFERS-REDUCED-MOTION: REDUCED) { a { color: red; } }",
    ):
        with pytest.raises(UnreadCss):
            color_of(read_sheet(unread), plain, "link", tablet)


def test_the_resolver_weighs_importance_and_reads_a_layout_in_any_case() -> None:
    first, second = links_in('<main><a class="x" href="/">x</a><a class="y" href="/">y</a></main>')
    weighed = read_sheet("a { display: block !important; } a.x { display: INLINE-FLEX; }")
    assert winning(weighed.displays, weighed, first, "link", View(320)) == "block"
    plain = read_sheet("a { display: block; } a.y { display: Inline-Block; }")
    assert winning(plain.displays, plain, second, "link", View(320)) == "inline-block"
    one_block = read_sheet("a { display: block !important; display: inline; }")
    assert winning(one_block.displays, one_block, second, "link", View(320)) == "block"
    named = read_sheet(":root { --Ink: #111111; --ink: #222222; } main a { color: var(--Ink); }")
    assert color_of(named, first, "link", View(320)) == "#111111"


def test_the_resolver_reads_a_comma_in_an_attribute_string_as_itself() -> None:
    (link,) = links_in('<main><a href="/" title="a,b">x</a></main>')
    sheet = read_sheet('main a[title="a,b"] { color: #888888; }')
    assert color_of(sheet, link, "link", View(320)) == "#888888"


def test_the_resolver_reads_no_other_letter_or_digit_as_ascii() -> None:
    with pytest.raises(UnreadCss):
        read_sheet("footer a { display: bloc\N{KELVIN SIGN}; color: #888888; }")
    with pytest.raises(UnreadCss):
        read_sheet("footer a { color: rgb(\u0661\u0662\u0660, 0, 0); }")


@pytest.mark.parametrize(
    ("tail", "token"),
    [
        ("#222222 !x", "#111111"),
        ("#222222 !importantx", "#111111"),
        ("f(a) !x", "#111111"),
        ("#222222) !x", "#111111"),
        ("f(!) #222222", None),
        ("f(!) a\\41", None),
        ("a\\( !x", "#111111"),
        ("url(a\\41 b)", None),
        ("url(a b)", "#111111"),
        ('"(" !x', "#111111"),
        ('f(")" !x)', None),
        ("'[' !x", "#111111"),
        ('f("]" !x)', None),
        ("url(a[) !x", "#111111"),
        ("f(url(a]) !x)", None),
        ("f(url(a) !x)", None),
        ("myurl(a b)", None),
        ("myURL(a b)", None),
        ("-url(a b)", None),
        ("_url(a b)", None),
        ("2url(a b)", None),
        ("(url(a b))", "#111111"),
        ("a url(a b)", "#111111"),
        ("a\nurl(a b)", "#111111"),
        ("a\r\nurl(a b)", "#111111"),
        ("a\nurl(a)", None),
        ("a\\\nurl(a b)", "#111111"),
        ("#url(a b)", None),
        ("@url(a b)", None),
        ("u\\72l (a b)", None),
        ("f([) !x])", "#111111"),
        ("[(] !x)]", "#111111"),
        ("f([)])", "#111111"),
        ("[f(]])]", "#111111"),
        ("a) b", "#111111"),
        ("a] b", "#111111"),
        ("(a] b)", "#111111"),
        (") f(!)", "#111111"),
        ("url(a)) b", "#111111"),
        ("f(g([h(])]))", "#111111"),
        ("f([a])", None),
        ("f(\\))", None),
        ("[\\)]", None),
        ('f(")")', None),
        ("url(a\\))", None),
        ('a\\"b', None),
        ("a\\'b", None),
    ],
)
def test_the_resolver_drops_a_custom_property_a_browser_drops(tail: str, token: str | None) -> None:
    """Edge drops a custom property with a `!` left outside any brackets once its
    `!important` is taken off, or with a `url()` it cannot read, and keeps one whose `!` is
    inside a function. An escape is text, an escaped bracket too, and so is a bracket in a
    string or a `url()`; `url(` ending a longer name is a function."""
    sheet = read_sheet(f":root {{ --x: #111111; }} :root {{ --x: {tail}; }}")
    assert sheet.tokens["x"] == (token or tail)


@pytest.mark.parametrize(
    "tail",
    [
        "\\61 url(a b)",
        "\\61\nurl(a b)",
        "\\000061\r\nurl(a b)",
        "a\\ url(a b)",
        "\\(url(a b)",
        "a\\url(a b)",
    ],
)
def test_the_resolver_refuses_an_escape_that_may_join_a_url(tail: str) -> None:
    """Edge reads `\\61 url(a b)` as a function named `aurl`, the escape joining the name."""
    with pytest.raises(UnreadCss):
        read_sheet(f":root {{ --x: {tail}; }}")


@pytest.mark.parametrize(
    "css",
    [
        ":root { --x: #111111; } :root { --x: u\\72l(a b); }",
        ":root { --x: #111111; } :root { --x: \\75rl(a b); }",
        ":root { --x: #111111; } :root { --x: ur\\6c (a b); }",
        ":root { --x: #111111; } :root { --x: \\55RL(a b); }",
        ":root { --x: #111111; } :root { --x: uR\\4c (a b); }",
        ":root { --x: #111111; } :root { --x: \\75 \\72 \\6c (a b); }",
        ":root { --x: #111111; } :root { --x: \\u\\r\\l(a b); }",
        ":root { --x: #111111; } :root { --x: u\\72l(a); }",
        ':root { --x: #111111; } :root { --x: u\\72l("a b"); }',
        ":root { --x: #111111; } :root { --x: xu\\72l(a b); }",
        ":root { --x: #111111; } :root { --x: f(u\\72l(a b)); }",
        ":root { --x: #111111; --x: u\\72l(a b",
    ],
)
def test_the_resolver_refuses_a_url_name_written_with_an_escape(css: str) -> None:
    """Edge reads `u\\72l(a b)` as a `url()` it cannot read and drops it, as it drops
    `url(a b)`."""
    with pytest.raises(UnreadCss):
        read_sheet(css)


@pytest.mark.parametrize(
    "css",
    [
        'main a { color: #888888; content: "abc',
        "main a { color: #888888; ",
        'main a { content: "abc\n; color: #888888; }',
        'main a { content: "abc\\\r\n; color: #111111; }"; color: #888888; }',
        'main a { content: "abc\\41\n; color: #111111; }"; color: #888888; }',
        "main a { color: #111111; width: calc(1px); color: #888888;",
        "main a { color: #111111; --x: a\\(; color: #888888; }",
        'main a { color: #111111; --x: "("; color: #888888; }',
    ],
)
def test_the_resolver_reads_text_left_open_as_a_browser_reads_it(css: str) -> None:
    (link,) = links_in('<main><a href="/w">w</a></main>')
    assert color_of(read_sheet(css), link, "link", View(320)) == "#888888"


@pytest.mark.parametrize(
    "css",
    [
        "main a { color: #888888; width: calc(1px; color: #111111;",
        'main a { color: #888888; background: url("a"; color: #111111;',
        ":root { --x: #111111; } :root { --x: f(a; b); }",
        ":root { --x: #111111; } :root { --x: f(a; --y: b",
    ],
)
def test_the_resolver_refuses_text_left_inside_a_bracket(css: str) -> None:
    """Edge keeps `#888888` for the link, and reads `--x` as `f(a; b)`, the `;` inside it."""
    with pytest.raises(UnreadCss):
        read_sheet(css)


@pytest.mark.parametrize("tail", ["f(a", "[a", "f([a]"])
def test_the_resolver_reads_a_bracket_open_at_the_end_as_a_browser_reads_it(tail: str) -> None:
    """Edge closes a bracket still open at the end of the sheet, and serializes `--x` as written."""
    assert read_sheet(f":root {{ --x: #111111; }} :root {{ --x: {tail}").tokens["x"] == tail


@pytest.mark.parametrize(
    ("css", "value"),
    [
        (":root { --x: f(!important", "f(!important"),
        (":root { --x: [!important", "[!important"),
        (":root { --x: #111111 !important; --x: f(!important", "#111111"),
        (":root { --x: #111111 !important; --x: [!important", "#111111"),
        (":root { --x: #111111 !important; --x: f(] !important", "#111111"),
        (":root { --x: #111111 !important; --x: f(a) !important", "f(a)"),
        (":root { --x: f(a) !important; --x: #111111", "f(a)"),
        (":root { --x: #111111 !important; --x: a\\( !important", "a\\("),
    ],
)
def test_the_resolver_reads_importance_inside_a_bracket_open_at_the_end_as_text(
    css: str, value: str
) -> None:
    """Edge reads `!important` inside a bracket open at the end of the sheet as its text."""
    assert read_sheet(css).tokens["x"] == value


@pytest.mark.parametrize(
    "css",
    [":root { --x: f(] !x); }", ":root { --x: [) !x]; }", ":root { --x: f(] !important"],
)
def test_the_resolver_drops_a_bang_after_a_closing_bracket_of_the_other_kind(css: str) -> None:
    """Edge drops `--x` here, the closing bracket of the other kind closing nothing."""
    assert "x" not in read_sheet(css).tokens


def test_the_resolver_refuses_a_bang_inside_a_substitution_open_at_the_end() -> None:
    """Edge drops `--x: var(--y, 2px !important` at the end of the sheet and keeps `#111111`."""
    with pytest.raises(UnreadCss):
        read_sheet(":root { --x: #111111; --x: var(--y, 2px !important")


@pytest.mark.parametrize(
    ("css", "token"),
    [
        (":root { --x: #111111 !important; } :root { --x: #222222; }", "#111111"),
        (":root { --x: #111111; } :root { --x: #222222 !important; }", "#222222"),
        (":root { --x: #111111 !important; } :root { --x: #222222 !important; }", "#222222"),
        (
            ":root { --x: #111111 !important; } :root { --x: #222222; } :root { --x: #333333; }",
            "#111111",
        ),
        (":root { --x: #111111 !important; } :root { --x: #222222; --x: #333333; }", "#111111"),
        (
            ":root { --x: #333333; } :root { --x: #111111 !important; } :root { --x: #222222; }",
            "#111111",
        ),
        (":root, .y { --x: #111111 !important; } :root { --x: #222222; }", "#111111"),
        (":root { --x: #111111 !important; } :root { --x: #222222 !x; }", "#111111"),
        (":root { --x: #111111 ! IMPORTANT; } :root { --x: #222222; }", "#111111"),
        (":root { --x: #111111; } :root { --x: #222222 !important", "#222222"),
        (":root { --x: #111111 !important; } :root { --x: #222222", "#111111"),
    ],
)
def test_the_resolver_weighs_importance_of_a_custom_property_across_rules(
    css: str, token: str
) -> None:
    """Edge gives `--x` its last important value over every normal one, whichever rule
    holds each."""
    assert read_sheet(css).tokens["x"] == token


def test_the_resolver_weighs_importance_of_each_custom_property_apart() -> None:
    sheet = read_sheet(
        ":root { --x: #111111 !important; --y: #333333; } :root { --x: #222222; --y: #444444; }"
    )
    assert (sheet.tokens["x"], sheet.tokens["y"]) == ("#111111", "#444444")


def test_a_link_takes_an_important_custom_property_set_in_an_earlier_rule() -> None:
    (link,) = links_in('<main><a href="/w">w</a></main>')
    css = ":root { --x: #111111 !important; } :root { --x: #888888; } main a { color: var(--x); }"
    assert color_of(read_sheet(css), link, "link", View(320)) == "#111111"


def test_the_contrast_check_reads_an_important_action_color_set_before_the_stylesheet() -> None:
    """Edge paints the darker action color `#999999` here, the important value winning over
    the stylesheet's later normal one, and that color is under 4.5 to 1 on a tint."""
    sheet = read_sheet(f":root {{ --blue-action-hover: #999999 !important; }}\n{stylesheet()}")
    assert sheet.tokens["blue-action-hover"] == "#999999"
    darker = drawn(sheet, "blue-action-hover")
    assert contrast(darker, drawn(sheet, "canvas", "rose-bg")) < 4.5


@pytest.mark.parametrize(
    "css",
    [
        "main a { color: #111111; } } p { color: #222222; } main a { color: #888888; }",
        "} main a { color: #111111; } main a { color: #888888; }",
    ],
)
def test_the_resolver_drops_only_the_rule_a_stray_closing_brace_starts(css: str) -> None:
    """Edge reads a `}` with no block open as part of the next rule's selector, drops that
    rule, and reads the rest."""
    (link,) = links_in('<main><a href="/w">w</a></main>')
    assert color_of(read_sheet(css), link, "link", View(320)) == "#888888"


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
TOUCH_HEIGHT_PX = 44
"""The least height of a control on the pages, a button's or a link's: 2.75rem at the root size."""
BOXED = frozenset({"block", "inline-block", "flex", "inline-flex", "grid", "inline-grid"})
"""The layouts that give a link a box of its own, which a minimum height applies to."""
IN_THE_LINE = frozenset({"inline-block", "inline-flex", "inline-grid"})
"""The boxed layouts that keep a link in the line of words around it."""
TO_THE_DETAILS = {"Details": BOXED, "Turning it in": IN_THE_LINE}
"""Each link on her week to an assignment's details, by its words, with the layouts it may take:
any box of its own for Details, and for Turning it in, at the end of the hand-in line, a box
that keeps it in the sentence."""
PLACES = frozenset(
    {
        "a card",
        "a card she reports done",
        "a row due later",
        "a row due later she reports done",
        "the card shown apart",
    }
)
"""Every place on her week that holds the links to an assignment's details."""
CARDS = PLACES - {"a row due later", "a row due later she reports done"}
ROWS = PLACES - CARDS
DETAILS_RULE = (
    ".details-link a,\na.details-link {\n  display: inline-flex;\n  align-items: center;\n"
    "  min-height: 2.75rem;\n  margin: 0;\n}"
)
HAND_IN_RULE = (
    ".hand-in-line a {\n  display: inline-flex;\n  align-items: center;\n  min-height: 2.75rem;\n}"
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


def links_named(pages: dict[str, str], words: str) -> list[Element]:
    return [
        link for page in pages.values() for link in links_in(page) if link.text.strip() == words
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
        raise UnreadCss(value)
    return float(found.group(1)) * (1 if found.group(2) == "px" else ROOT_FONT_PX)


def short_of_a_control(sheet: Sheet, link: Element, view: View, layouts: frozenset[str]) -> bool:
    """Whether ``link`` on ``view`` is in none of ``layouts``, as an inline link always is, or
    has no minimum height or one under a control's."""
    display = winning(sheet.displays, sheet, link, "link", view)
    least = winning(sheet.heights, sheet, link, "link", view)
    return display not in layouts or least is None or height_px(least) < TOUCH_HEIGHT_PX


@pytest.mark.parametrize("width", WIDTHS)
@pytest.mark.parametrize("words", list(TO_THE_DETAILS))
def test_every_link_to_the_details_is_as_tall_as_a_control(
    her_weeks: dict[str, str], words: str, width: int
) -> None:
    """In every place on her week, at each width, with or without less motion: 44 pixels."""
    sheet = read_sheet(stylesheet())
    found = links_named(her_weeks, words)

    assert {place_of(link) for link in found} == PLACES
    for view in (View(width), View(width, reduced_motion=True)):
        for link in found:
            where = (place_of(link), link.attributes["aria-label"], view)
            assert not short_of_a_control(sheet, link, view, TO_THE_DETAILS[words]), where


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
    found = links_named(her_weeks, "Details")

    assert {place_of(link) for link in found} == PLACES
    for link in found:
        for width in WIDTHS:
            expected = place_of(link) in short and width > fine_up_to
            is_short = short_of_a_control(sheet, link, View(width), BOXED)
            assert is_short == expected, (place_of(link), width)


@pytest.mark.parametrize(
    ("becomes", "fine_up_to"),
    [
        pytest.param(
            HAND_IN_RULE.replace(".hand-in-line a {", ".hand-in-line b {"), 0, id="selector-broken"
        ),
        pytest.param(HAND_IN_RULE.replace("inline-flex", "inline"), 0, id="laid-out-inline"),
        pytest.param(HAND_IN_RULE.replace("inline-flex", "flex"), 0, id="out-of-its-sentence"),
        pytest.param(HAND_IN_RULE.replace("2.75rem", "1.5rem"), 0, id="too-short"),
        pytest.param(
            f"@media (max-width: 30rem) {{\n{HAND_IN_RULE}\n}}", 480, id="only-on-a-phone"
        ),
    ],
)
def test_the_height_check_fails_when_the_hand_in_rule_stops_doing_its_work(
    her_weeks: dict[str, str], becomes: str, fine_up_to: int
) -> None:
    """Every Turning it in link is short of a control on each screen the broken rule leaves it."""
    css = stylesheet()
    assert css.count(HAND_IN_RULE) == 1
    sheet = read_sheet(css.replace(HAND_IN_RULE, becomes))
    found = links_named(her_weeks, "Turning it in")

    assert {place_of(link) for link in found} == PLACES
    for link in found:
        for width in WIDTHS:
            is_short = short_of_a_control(sheet, link, View(width), IN_THE_LINE)
            assert is_short == (width > fine_up_to), (place_of(link), width)


SCIENCE_ID = "assignment-science-fair-proposal"
COVER_ID = "assignment-textbook-cover"
HER_ROWS = frozenset({"a row of her To turn in list"})
UPDATES = frozenset(
    {
        "a row worth checking together",
        "a row checked recently",
        "a recent update",
        "a school report",
        "a row turning work in",
    }
)
"""Each group of the family page's updates, and the rows of what she says about turning work in."""
LEAD = frozenset({"the way to adding assignments"})
UNDER_THE_NOTES = frozenset({"the line under the homework notes"})
NOTE_ROWS = frozenset({"a homework note's row"})
LISTED = HER_ROWS | UPDATES | LEAD | UNDER_THE_NOTES | NOTE_ROWS
LIST_PLACES = frozenset(
    {
        ("her week", "a row of her To turn in list"),
        ("her week", "the line under the homework notes"),
        ("her week", "a homework note's row"),
        ("her To turn in list", "a row of her To turn in list"),
        ("her homework notes", "a homework note's row"),
        *(("the family page", place) for place in UPDATES | LEAD | UNDER_THE_NOTES),
    }
)
"""Every page and place that holds a link the lists rule is for."""
LISTS_RULE = (
    ".to-turn-in-row a.assignment-link,\n.assignment-updates a.assignment-link,\n"
    ".lead-action a,\n.homework-notes > .note a,\n.homework-note-row a.assignment-link {\n"
    "  display: inline-flex;\n  align-items: center;\n  min-height: 2.75rem;\n}"
)
LISTS: dict[str, str] = {}


@pytest.fixture
def lists() -> dict[str, str]:
    """Her week, her To turn in list, her homework notes, and the family page, with two
    assignments still to turn in, four homework notes, and a row in every group of the
    updates. Kept once rendered, as ``rendered`` is."""
    if LISTS:
        return LISTS
    with browser() as client:
        store = state_of(client).project_state
        for name in (ESSAY_ID, ALGEBRA_ID, COVER_ID):
            report(client, name, "done")
        for name in (ESSAY_ID, ALGEBRA_ID, QUIZ_ID):
            store.record_status_reports(
                name, [school_said("missing", SourceChannel.EMAIL, PLAN_DATE)]
            )
        family = client.get("/parent", headers=PAGE_HEADERS).text
        row = family[family.index(f'id="update-{ESSAY_ID}"') :]
        checked = client.post(
            f"/parent/actions/checks/{ESSAY_ID}/mark",
            data={
                "basis": hidden(row, "basis"),
                "expected_check_id": hidden(row, "expected_check_id"),
                "note": "",
            },
            headers=PAGE_HEADERS,
        )
        assert checked.status_code == 303, checked.text[:400]
        for name in (SCIENCE_ID, LOG_ID):
            kept = store.record_hand_in(
                name,
                NEEDS_HAND_IN,
                None,
                None,
                expected_head=None,
                now=datetime(2026, 8, 19, 21, 0, tzinfo=UTC),
                today=PLAN_DATE,
            )
            assert isinstance(kept, HandInSaved), kept
        for number in range(1, 5):
            fields = form_fields(client.get(NEW_NOTE_PAGE).text, NOTE_ACTIONS)
            saved = client.post(
                NOTE_ACTIONS,
                data={
                    **fields,
                    "text": f"Geometry questions, page {number}",
                    "course": "",
                    "due_date": "",
                },
                headers=PAGE_HEADERS,
            )
            assert saved.status_code == 303, saved.text[:400]
        LISTS.update(
            {
                "her week": client.get(HER_PAGE, headers=PAGE_HEADERS).text,
                "her To turn in list": client.get(TO_TURN_IN_PAGE, headers=PAGE_HEADERS).text,
                "her homework notes": client.get(NOTES_PAGE, headers=PAGE_HEADERS).text,
                "the family page": client.get("/parent", headers=PAGE_HEADERS).text,
            }
        )
    return LISTS


def list_place(link: Element) -> str | None:
    """Where a link the lists rule is for stands, read from the elements above it, or ``None``
    for any other link."""
    above = link.ancestors()
    marks = {name for item in above for name in item.classes}
    line = link.parent
    if (
        line is not None
        and "note" in line.classes
        and line.parent is not None
        and "homework-notes" in line.parent.classes
    ):
        return "the line under the homework notes"
    if "lead-action" in marks:
        return "the way to adding assignments"
    if "assignment-link" not in link.classes:
        return None
    if "homework-note-row" in marks:
        return "a homework note's row"
    if "to-turn-in-row" in marks:
        return "a row of her To turn in list"
    if "assignment-updates" not in marks:
        return None
    if any(item.attributes.get("id") == "turning-work-in" for item in above):
        return "a row turning work in"
    if "checked-recently" in marks:
        return "a row checked recently"
    if any(item.tag == "details" for item in above):
        return "a recent update"
    return "a row worth checking together" if "needs-review" in marks else "a school report"


def links_in_the_lists(pages: dict[str, str]) -> list[tuple[str, str, Element]]:
    """Each link the lists rule is for, with its page and its place."""
    return [
        (name, place, link)
        for name, page in pages.items()
        for link in links_in(page)
        if (place := list_place(link)) is not None
    ]


@pytest.mark.parametrize("width", WIDTHS)
def test_every_title_and_line_of_links_in_the_lists_is_as_tall_as_a_control(
    lists: dict[str, str], width: int
) -> None:
    """In every place, at each width, with or without less motion: 44 pixels, in its line."""
    sheet = read_sheet(stylesheet())
    found = links_in_the_lists(lists)

    assert {(name, place) for name, place, _ in found} == LIST_PLACES
    for view in (View(width), View(width, reduced_motion=True)):
        for name, place, link in found:
            where = (name, place, link.text.strip(), view)
            assert not short_of_a_control(sheet, link, view, IN_THE_LINE), where


@pytest.mark.parametrize(
    ("becomes", "short", "fine_up_to"),
    [
        pytest.param(
            LISTS_RULE.replace(".to-turn-in-row a.", ".to-turn-in-row b."),
            HER_ROWS,
            0,
            id="her-rows-selector-broken",
        ),
        pytest.param(
            LISTS_RULE.replace(".assignment-updates a.", ".assignment-updates b."),
            UPDATES,
            0,
            id="the-updates-selector-broken",
        ),
        pytest.param(
            LISTS_RULE.replace(".lead-action a,", ".lead-action b,"),
            LEAD,
            0,
            id="the-lead-selector-broken",
        ),
        pytest.param(
            LISTS_RULE.replace(".note a,", ".note b,"),
            UNDER_THE_NOTES,
            0,
            id="the-notes-selector-broken",
        ),
        pytest.param(
            LISTS_RULE.replace(".homework-note-row a.", ".homework-note-row b."),
            NOTE_ROWS,
            0,
            id="the-note-rows-selector-broken",
        ),
        pytest.param(LISTS_RULE.replace("inline-flex", "inline"), LISTED, 0, id="laid-out-inline"),
        pytest.param(LISTS_RULE.replace("inline-flex", "flex"), LISTED, 0, id="out-of-its-line"),
        pytest.param(LISTS_RULE.replace("2.75rem", "1.5rem"), LISTED, 0, id="too-short"),
        pytest.param(
            f"@media (max-width: 30rem) {{\n{LISTS_RULE}\n}}", LISTED, 480, id="only-on-a-phone"
        ),
    ],
)
def test_the_height_check_fails_when_the_lists_rule_stops_doing_its_work(
    lists: dict[str, str], becomes: str, short: frozenset[str], fine_up_to: int
) -> None:
    """Exactly the links the broken rule leaves fall short of a control, where it leaves them."""
    css = stylesheet()
    assert css.count(LISTS_RULE) == 1
    sheet = read_sheet(css.replace(LISTS_RULE, becomes))
    found = links_in_the_lists(lists)

    assert {(name, place) for name, place, _ in found} == LIST_PLACES
    for name, place, link in found:
        for width in WIDTHS:
            expected = place in short and width > fine_up_to
            is_short = short_of_a_control(sheet, link, View(width), IN_THE_LINE)
            assert is_short == expected, (name, place, width)


TEXT_PX = (20.0, 22.0)
"""The least and the most height an inline link's words take in the body font at the root
size, the box its padding is added to: Edge draws it 20 or 21 pixels tall, and a pixel is
kept to spare."""
TO_CHECK_TITLES = (
    "Comparing the Canal Era and the railroads that followed it",
    "Lab report on plant growth under colored light",
    "Chapter review questions for the unit on fractions and decimals",
    "Reading log for the independent novel, chapters one through six",
    "Map of the watershed with labeled tributaries and towns",
    "Vocabulary sentences for words eleven through twenty",
)
ITEM_RULE = (
    ".to-check-item {\n  display: inline-block;\n  padding: 0.8rem 0.3em 0.8rem 0;\n"
    "  pointer-events: none;\n}"
)
MARK_RULE = ".to-check-mark {\n  display: inline-block;\n  width: 0;\n}"
LINK_RULE = ".to-check a {\n  padding: 0.8rem 0;\n  pointer-events: auto;\n}"
TO_CHECK_RULES = f"{ITEM_RULE}\n\n{MARK_RULE}\n\n{LINK_RULE}"
TO_CHECK: dict[int, str] = {}


def finished_and_missing(store: ProjectStateStore, count: int) -> list[str]:
    """``count`` assignments with long titles, due this week, each reported done and reported
    missing by the school, so that many school reports to check; their ids, in due order."""
    names = [f"assignment-to-check-{number}" for number in range(count)]
    store.put_on_record(
        [
            due(name, title, PLAN_DATE + timedelta(days=1 + number % 3))
            for number, (name, title) in enumerate(zip(names, TO_CHECK_TITLES, strict=False))
        ],
        {},
    )
    for name in names:
        reported(store, "done", name)
        store.record_status_reports(name, [school_said("missing", SourceChannel.EMAIL, PLAN_DATE)])
    return names


@pytest.fixture
def to_check() -> dict[int, str]:
    """Her week with one, two, three and six school reports to check. Kept once rendered, as
    ``rendered`` is."""
    if TO_CHECK:
        return TO_CHECK
    for count in (1, 2, 3, 6):
        with browser() as client:
            finished_and_missing(state_of(client).project_state, count)
            TO_CHECK[count] = client.get(HER_PAGE, headers=PAGE_HEADERS).text
    return TO_CHECK


def links_to_check(page: str) -> list[Element]:
    """The links in her week's line of school reports to check: a notice of its own in the
    page's main part, whatever holds each link inside it."""
    return [
        link
        for link in links_in(page)
        if any(
            "confidence" in above.classes
            and above.parent is not None
            and above.parent.tag == "main"
            for above in link.ancestors()
        )
    ]


def top_and_bottom(value: str | None) -> tuple[float, float]:
    """The top and bottom of a padding shorthand in CSS pixels; none set is none."""
    if value is None:
        return (0.0, 0.0)
    sides = value.split()
    if not 1 <= len(sides) <= 4:
        raise UnreadCss(value)
    top, bottom = sides[0], sides[2] if len(sides) > 2 else sides[0]
    return (
        0.0 if top == "0" else height_px(top),
        0.0 if bottom == "0" else height_px(bottom),
    )


def held_in_its_item(sheet: Sheet, link: Element, view: View) -> bool:
    """Whether ``link`` on ``view`` stays an inline box in its words, padded above and below to
    at least a control's height, inside an item of the line that is a box of its own in the
    sentence, padded at least as far as the link. The line then grows around the item, and
    the link's area stays inside it whatever line height, letter spacing or word spacing a
    reader sets, so it reaches into no other link's."""
    item = link.parent
    assert item is not None
    display = winning(sheet.displays, sheet, link, "link", view)
    padding = top_and_bottom(winning(sheet.paddings, sheet, link, "link", view))
    holds = winning(sheet.displays, sheet, item, "link", view)
    room = top_and_bottom(winning(sheet.paddings, sheet, item, "link", view))
    return (
        display in (None, "inline")
        and TEXT_PX[0] + sum(padding) >= TOUCH_HEIGHT_PX
        and holds in IN_THE_LINE
        and room[0] >= padding[0]
        and room[1] >= padding[1]
    )


def test_the_school_reports_to_check_are_in_text_of_the_root_size() -> None:
    """The size the heights here are worked out for: no rule on the notice, its line, its items
    or its links sets a text size of its own."""
    for rule in ("body", ".confidence", ".confidence.disagree", ".to-check", ".to-check a"):
        for inside in rules_named(rule):
            assert "font-size" not in inside, rule
    for inside in rules_named(".to-check-item"):
        assert "font-size" not in inside


def test_the_line_leaves_a_reader_s_own_spacing_alone() -> None:
    """Nothing on the line, its items or its links sets line height, letter or word spacing,
    or marks a declaration important, so a reader's own stylesheet always has its way."""
    for rule in (
        ".confidence",
        ".confidence.disagree",
        ".to-check",
        ".to-check a",
        ".to-check-item",
    ):
        for inside in rules_named(rule):
            for held in ("line-height", "letter-spacing", "word-spacing", "!important"):
                assert held not in inside, (rule, held)


def test_the_punctuation_takes_no_press_and_each_link_takes_every_press_to_its_edge() -> None:
    """The glyph of a comma can reach over the last pixel of the link before it, and a press
    there would go to the comma's text. The item takes no press and the link takes them all,
    so every press inside the link's area is the link's."""
    assert any("pointer-events: none;" in inside for inside in rules_named(".to-check-item"))
    assert any("pointer-events: auto;" in inside for inside in rules_named(".to-check a"))


def test_the_punctuation_takes_no_width_of_the_line_and_shows_in_room_the_item_keeps() -> None:
    """A word that fills a line could otherwise push the comma after it onto a line of its own.
    In a box of no width the comma always fits on the line of the link's last word, and its
    glyph shows in the room the item keeps at its end."""
    marks = rules_named(".to-check-mark")
    room = [
        inside.split("padding:", 1)[1].split(";", 1)[0].split()
        for inside in rules_named(".to-check-item")
        if "padding:" in inside
    ]

    assert any("display: inline-block;" in inside and "width: 0;" in inside for inside in marks)
    assert room
    assert all(len(sides) == 4 and sides[1] != "0" for sides in room), room


@pytest.mark.parametrize("width", WIDTHS)
def test_each_school_report_to_check_is_as_tall_as_a_control_inside_its_own_item(
    to_check: dict[int, str], width: int
) -> None:
    """With one link or six, at each width, with or without less motion: 44 pixels, in the
    words of the sentence, and inside an item that keeps it from every other link."""
    sheet = read_sheet(stylesheet())

    for count, page in to_check.items():
        found = links_to_check(page)
        assert len(found) == count
        for link in found:
            assert link.attributes["aria-label"].endswith(": school report to check"), link.text
            for view in (View(width), View(width, reduced_motion=True)):
                assert held_in_its_item(sheet, link, view), (count, link.text, view)


@pytest.mark.parametrize("reader", ["her", "parent", "open"])
def test_each_school_report_to_check_sits_with_its_punctuation_in_an_item(
    reader: str, tmp_path: pathlib.Path
) -> None:
    """The line reads as the sentence it is, the words and punctuation in order, and each link
    sits in an item of its own with the comma or period after it, for her, a parent and the
    sign-in off."""
    with household_client(reader, tmp_path) as client:
        sign_in_as(client, reader)
        finished_and_missing(state_of(client).project_state, 3)
        page = client.get(HER_PAGE, headers=PAGE_HEADERS).text
    line = re.search(r'<p class="confidence disagree to-check" role="status">(.*?)</p>', page, re.S)
    assert line is not None
    items = re.findall(
        r'<span class="to-check-item"><a\b[^>]*>([^<]*)</a>'
        r'<span class="to-check-mark">([,.])</span></span>',
        line[1],
    )
    titles = [unescape(title) for title, _ in items]
    as_read = " ".join(unescape(re.sub(r"<[^>]+>", "", line[1])).split())

    assert sorted(titles) == sorted(TO_CHECK_TITLES[:3])
    assert [mark for _, mark in items] == [",", ",", "."]
    assert as_read == f"3 finished assignments have school reports to check: {', '.join(titles)}."
    assert len(re.findall(r"<a\b", line[1])) == 3


@pytest.mark.parametrize(
    ("becomes", "fine_up_to"),
    [
        pytest.param(
            TO_CHECK_RULES.replace(".to-check-item {", ".to-check-items {"), 0, id="items-left"
        ),
        pytest.param(
            TO_CHECK_RULES.replace(ITEM_RULE, ITEM_RULE.replace("inline-block", "inline")),
            0,
            id="items-in-the-line-box",
        ),
        pytest.param(
            TO_CHECK_RULES.replace(ITEM_RULE, ITEM_RULE.replace("inline-block", "block")),
            0,
            id="items-out-of-the-sentence",
        ),
        pytest.param(
            TO_CHECK_RULES.replace(
                ITEM_RULE, ITEM_RULE.replace("0.8rem 0.3em 0.8rem", "0.5rem 0.3em 0.5rem")
            ),
            0,
            id="items-padded-short",
        ),
        pytest.param(
            TO_CHECK_RULES.replace(
                ITEM_RULE, ITEM_RULE.replace("  padding: 0.8rem 0.3em 0.8rem 0;\n", "")
            ),
            0,
            id="items-not-padded",
        ),
        pytest.param(TO_CHECK_RULES.replace(".to-check a {", ".to-check b {"), 0, id="links-left"),
        pytest.param(
            TO_CHECK_RULES.replace(LINK_RULE, LINK_RULE.replace("0.8rem 0", "0.5rem 0")),
            0,
            id="links-too-short",
        ),
        pytest.param(
            TO_CHECK_RULES.replace(
                LINK_RULE, LINK_RULE.replace("  padding:", "  display: inline-block;\n  padding:")
            ),
            0,
            id="links-out-of-their-words",
        ),
        pytest.param(
            f"@media (max-width: 30rem) {{\n{TO_CHECK_RULES}\n}}", 480, id="only-on-a-phone"
        ),
    ],
)
def test_the_check_fails_when_the_school_reports_rules_stop_doing_their_work(
    to_check: dict[int, str], becomes: str, fine_up_to: int
) -> None:
    """Every link to check falls short on each screen the broken rules leave it."""
    css = stylesheet()
    assert css.count(TO_CHECK_RULES) == 1
    sheet = read_sheet(css.replace(TO_CHECK_RULES, becomes))

    for page in to_check.values():
        for link in links_to_check(page):
            kept = [held_in_its_item(sheet, link, View(width)) for width in WIDTHS]
            assert kept == [width <= fine_up_to for width in WIDTHS], link.text
