"""What is kept of the words a person writes into a field, and what is refused whole.

Her hand-in notes, and the fields that follow them, keep a person's words as
written. Line endings become one kind, the edges are trimmed, and blank is
nothing, so the same words typed on two devices compare equal. Nothing else
is ever taken out: text that holds something the record will not keep is
refused as it is, for the person to correct, and the refusal says which rule
it met.

Refused: a control character other than a line feed or a tab, wherever it
sits, since trimming would take some of them off an edge before anyone saw
them; and a surrogate, half of a character that cannot be written to the file
or shown. Kept: characters that join or shape others, an emoji family's
joiners among them, and the replacement character a browser sends for bytes
it could not read, which is one ordinary character here and says nothing
about how it came. A field of one line also holds no tab, line feed, or
Unicode line or paragraph separator inside it. Length is counted in code
points after trimming; a browser's own limit counts differently and is not
the rule.

The notes her updates and the family's checks already keep are read by their
own rule, in the store, and rows written under it stay readable.
"""

import unicodedata
from typing import Final, Literal

Reason = Literal["control", "surrogate", "line_break", "too_long"]
CONTROL: Final = "control"
SURROGATE: Final = "surrogate"
LINE_BREAK: Final = "line_break"
TOO_LONG: Final = "too_long"

LINE_FEED: Final = "\n"
TAB: Final = "\t"
ONE_LINE_BREAKS: Final = frozenset({LINE_FEED, TAB, "\u2028", "\u2029"})


class TextRefused(ValueError):
    """Raised for text the record will not keep, with the rule it met."""

    def __init__(self, reason: Reason, *, length: int = 0, limit: int = 0) -> None:
        self.reason: Reason = reason
        self.length = length
        self.limit = limit
        super().__init__(
            {
                "control": "the text holds a control character",
                "surrogate": "the text holds half of a character",
                "line_break": "one line of text holds a line break or a tab",
                "too_long": f"the text is at most {limit} characters; this is {length}",
            }[reason]
        )


def _trimmed(text: str | None) -> str | None:
    """Line endings as one kind, every character checked where it sits, then the edges off."""
    if text is None:
        return None
    unified = text.replace("\r\n", LINE_FEED).replace("\r", LINE_FEED)
    for character in unified:
        category = unicodedata.category(character)
        if category == "Cs":
            raise TextRefused(SURROGATE)
        if category == "Cc" and character not in (LINE_FEED, TAB):
            raise TextRefused(CONTROL)
    return unified.strip() or None


def _within(text: str | None, limit: int) -> str | None:
    if text is not None and len(text) > limit:
        raise TextRefused(TOO_LONG, length=len(text), limit=limit)
    return text


def multiline(text: str | None, limit: int) -> str | None:
    """Text of several lines as it is kept, or ``TextRefused``; blank is ``None``."""
    return _within(_trimmed(text), limit)


def single_line(text: str | None, limit: int) -> str | None:
    """One line of text as it is kept, or ``TextRefused``; blank is ``None``.

    A break at an edge is trimmed like any other edge; one inside the line is
    refused, since a title or a next action is shown on one line.
    """
    trimmed = _trimmed(text)
    if trimmed is not None and any(character in ONE_LINE_BREAKS for character in trimmed):
        raise TextRefused(LINE_BREAK)
    return _within(trimmed, limit)
