"""The words a person writes into the newer fields: what is kept, and what is refused whole.

Nothing is ever taken out of what she wrote. Text that holds something the
record will not keep is refused as it is, for her to correct, and the reason
says which rule it met.
"""

import pytest

from blossom.authored_text import TextRefused, multiline, single_line

ZERO_WIDTH_JOINER = "\u200d"
FAMILY = f"\U0001f469{ZERO_WIDTH_JOINER}\U0001f469{ZERO_WIDTH_JOINER}\U0001f467"
REPLACEMENT = "\ufffd"
LINE_SEPARATOR = "\u2028"
PARAGRAPH_SEPARATOR = "\u2029"
LONE_SURROGATE = "\ud800"


def test_line_endings_become_one_kind_edges_go_and_blank_is_nothing() -> None:
    assert multiline("  first\r\nsecond\rthird\n", 500) == "first\nsecond\nthird"
    assert multiline(" \r\n\t ", 500) is None
    assert multiline(None, 500) is None
    assert single_line("  Put it in the folder \n", 200) == "Put it in the folder"
    assert single_line("   ", 200) is None


def test_what_is_inside_stays_as_written() -> None:
    assert multiline("a\tb\n\n  c", 500) == "a\tb\n\n  c"
    assert multiline(f"for {FAMILY}", 500) == f"for {FAMILY}"
    assert multiline(f"said {REPLACEMENT} here", 500) == f"said {REPLACEMENT} here"


def test_the_limit_counts_code_points_after_the_edges_are_trimmed() -> None:
    assert multiline("  " + "\U0001f600" * 500 + "\n", 500) == "\U0001f600" * 500
    assert multiline(REPLACEMENT * 500, 500) == REPLACEMENT * 500
    with pytest.raises(TextRefused) as refusal:
        multiline("x" * 501, 500)
    assert refusal.value.reason == "too_long"
    assert refusal.value.length == 501
    assert refusal.value.limit == 500
    with pytest.raises(TextRefused, match="200"):
        single_line("x" * 201, 200)


@pytest.mark.parametrize("control", ["\x00", "\x08", "\x0b", "\x1b", "\x1f", "\x7f", "\x85"])
def test_a_control_character_is_refused_wherever_it_sits(control: str) -> None:
    """The edge included: trimming would take some of them off before anyone saw them."""
    for text in (f"note {control} here", f"{control}note", f"note{control}"):
        with pytest.raises(TextRefused) as refusal:
            multiline(text, 500)
        assert refusal.value.reason == "control"
        with pytest.raises(TextRefused):
            single_line(text, 200)


def test_a_lone_surrogate_is_refused() -> None:
    with pytest.raises(TextRefused) as refusal:
        multiline(f"note {LONE_SURROGATE}", 500)
    assert refusal.value.reason == "surrogate"


@pytest.mark.parametrize("breaker", ["\n", "\t", LINE_SEPARATOR, PARAGRAPH_SEPARATOR, "\r\n"])
def test_one_line_holds_no_break_inside_it(breaker: str) -> None:
    with pytest.raises(TextRefused) as refusal:
        single_line(f"Put it{breaker}in the folder", 200)
    assert refusal.value.reason == "line_break"
    assert single_line(f"Put it in the folder{breaker}", 200) == "Put it in the folder"


def test_several_lines_may_hold_the_separators_one_line_may_not() -> None:
    assert multiline(f"one{LINE_SEPARATOR}two", 500) == f"one{LINE_SEPARATOR}two"
