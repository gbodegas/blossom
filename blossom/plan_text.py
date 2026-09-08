"""The saved plan, taken apart for the page the way the composer put it together.

A draft is text, and the text is the record: the planner's blocks, their
reasons, what waits, what needs a word with someone, and the reviewer's notes,
in the shapes ``compose_draft`` writes. Reading those shapes back lets a page
set the time range in bold, the reason under it, and the reviewer's notes
behind a fold, without changing a character of what was saved. A line the
reader does not recognize is kept as it is, so a draft in a shape this module
has never seen is shown whole rather than dropped.

Nothing here decides anything. It is presentation of the record, not a second
reading of the plan.
"""

import re
from dataclasses import dataclass, field

REVIEW_HEADING = "The reviewer's notes:"
NOTHING_SCHEDULED = "Nothing is scheduled tonight."

BLOCK = re.compile(
    r"^(?P<span>\d{1,2}:\d{2}(?: [AP]M)? to \d{1,2}:\d{2}(?: [AP]M)?), set aside for (?P<item>.+)$"
)
"""A block line as the composer writes it, with either clock form."""

EARLIER_BLOCK = re.compile(r"^(?P<span>\d{2}:\d{2} to \d{2}:\d{2})  (?P<item>.+)$")
"""The other shape a saved block line can have: the time range, two spaces, the item."""

FINDING = re.compile(
    r"^(?P<label>[^:]+ \((?:passes|does not pass|could not assess|PASSES|FAILS|CANNOT_TELL)\)):"
    r"\s*(?P<text>.*)$"
)
"""One of the reviewer's findings: the criterion and its verdict, then the critique."""


@dataclass(frozen=True)
class Block:
    """One reserved period: when, for what, and the reason under it."""

    span: str
    item: str
    rationale: str = ""


@dataclass(frozen=True)
class Item:
    """One line of a list, with the part before the colon set apart when it names a finding."""

    text: str
    label: str | None = None


@dataclass
class Section:
    """A heading and the lines under it."""

    heading: str
    items: list[Item] = field(default_factory=list)

    @property
    def title(self) -> str:
        """The heading without its colon."""
        return self.heading.rstrip(":")


@dataclass
class PlanText:
    """The draft's parts, in the order the composer wrote them."""

    title: str
    notes: list[str] = field(default_factory=list)
    blocks: list[Block] = field(default_factory=list)
    empty: str | None = None
    sections: list[Section] = field(default_factory=list)
    review: Section | None = None
    other: list[str] = field(default_factory=list)
    """Lines in no shape this reader knows and with nothing before them to continue, kept whole."""


def present_plan(body: str) -> PlanText:
    """Take the saved text apart along the composer's shapes. Nothing is lost or reworded."""
    lines = body.split("\n")
    text = PlanText(title=lines[0] if lines else "")
    index = 1
    while index < len(lines) and lines[index].strip():
        text.notes.append(lines[index])
        index += 1
    current: Section | None = None
    for line in lines[index:]:
        if not line.strip():
            continue
        block = BLOCK.match(line) or EARLIER_BLOCK.match(line)
        if block and current is None:
            text.blocks.append(Block(span=block["span"], item=block["item"]))
        elif line.startswith("    ") and text.blocks and current is None:
            last = text.blocks[-1]
            rationale = f"{last.rationale} {line.strip()}".strip()
            text.blocks[-1] = Block(span=last.span, item=last.item, rationale=rationale)
        elif line == NOTHING_SCHEDULED and current is None:
            text.empty = line
        elif line.endswith(":") and not line.startswith("- "):
            current = Section(heading=line)
            if line == REVIEW_HEADING:
                text.review = current
            else:
                text.sections.append(current)
        elif line.startswith("- ") and current is not None:
            finding = FINDING.match(line[2:])
            if finding:
                current.items.append(Item(text=finding["text"], label=finding["label"]))
            else:
                current.items.append(Item(text=line[2:]))
        elif current is not None and current.items:
            # A value written with a line break inside it continues the item
            # it belongs to, in place, rather than drifting to the end.
            last_item = current.items[-1]
            current.items[-1] = Item(text=f"{last_item.text} {line.strip()}", label=last_item.label)
        elif current is None and text.blocks:
            last_block = text.blocks[-1]
            rationale = f"{last_block.rationale} {line.strip()}".strip()
            text.blocks[-1] = Block(span=last_block.span, item=last_block.item, rationale=rationale)
        else:
            text.other.append(line.strip())
    return text
