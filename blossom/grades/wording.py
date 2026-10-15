# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Gerardo Bodegas Martinez
"""The words a page writes for a school value: a cell as written or by its presence, a report
by the day it was added, and the school score line.

Every page that shows a school score writes it through ``score_line``, so one value reads the
same wherever it shows. Nothing here reads a store or a clock: the caller passes each value,
the reports of its class and term, and the household's zone.
"""

from collections.abc import Mapping
from datetime import date, datetime, tzinfo
from typing import Final

from blossom.grades.draft import Presence
from blossom.grades.review import Cell, ClassReport, CurrentValue, GradeValueUndecodable


def described(presence: Presence, text: str, label: str | None, *, named: bool = False) -> str:
    """A cell's text and presence as the review reads them out: as written, or what the presence
    says, short after a label the page writes and under ``label`` where no label introduces it.
    ``named`` puts the label before a reported value too."""
    if presence is Presence.REPORTED:
        return f"{label} {text}" if named and label else text
    said = {
        Presence.BLANK: ("blank", "left blank"),
        Presence.UNREADABLE: (f"couldn't be read: {text}", f"couldn't be read: {text}"),
        Presence.NOT_CAPTURED: ("not in the copy", "not in the copy"),
    }[presence]
    return said[0] if label is None else f"{label} {said[1]}"


def presence_words(what: str, cell: Cell) -> str:
    """A cell that wasn't reported, in the words for its presence."""
    presence, text = cell
    if presence is Presence.BLANK:
        return f"{what} left blank"
    if presence is Presence.UNREADABLE:
        return f"{what} couldn't be read: {text}"
    return f"{what} not in the copy"


def as_percent(text: str) -> str:
    """A percent the school reported, as written, with its sign."""
    return text if text.endswith("%") else f"{text}%"


ABSENT_ALIKE: Final = frozenset({Presence.BLANK, Presence.NOT_CAPTURED})
"""The presences a line names once when every cell of it holds the same one."""


def absent_alike(*cells: Cell) -> Cell | None:
    """The first of ``cells`` when all were left blank, or all are not in the copy; None when a
    line has to say each of them."""
    first = cells[0][0]
    if first in ABSENT_ALIKE and all(cell[0] is first for cell in cells):
        return cells[0]
    return None


def as_said(cell: Cell, label: str) -> str:
    """A percent cell of a line: as written, with its sign, or the review's words for a cell
    under ``label`` that wasn't reported."""
    if cell[0] is Presence.REPORTED:
        return as_percent(cell[1])
    return described(*cell, label)


UNITS: Final = (
    "",
    "one",
    "two",
    "three",
    "four",
    "five",
    "six",
    "seven",
    "eight",
    "nine",
    "ten",
    "eleven",
    "twelve",
    "thirteen",
    "fourteen",
    "fifteen",
    "sixteen",
    "seventeen",
    "eighteen",
    "nineteen",
)
"""The counts below twenty, spelled."""

TENS: Final = ("", "", "twenty", "thirty", "forty", "fifty", "sixty", "seventy", "eighty", "ninety")
"""Each ten from twenty up, spelled."""

SCALES: Final = (
    (10**12, "trillion"),
    (10**9, "billion"),
    (10**6, "million"),
    (1000, "thousand"),
    (100, "hundred"),
)
"""The words for counts of a hundred and more, largest first."""

PLACE_WORDS: Final = {
    "one": "first",
    "two": "second",
    "three": "third",
    "five": "fifth",
    "eight": "eighth",
    "nine": "ninth",
    "twelve": "twelfth",
}
"""The spelled counts whose place word isn't the count with "th" after it."""


def spelled(count: int) -> str:
    """A positive count in words, as "twenty-one" or "one hundred one"."""
    for size, word in SCALES:
        if count >= size:
            rest = count % size
            return f"{spelled(count // size)} {word}" + (f" {spelled(rest)}" if rest else "")
    if count < len(UNITS):
        return UNITS[count]
    rest = count % 10
    return TENS[count // 10] + (f"-{UNITS[rest]}" if rest else "")


def ordinal(count: int) -> str:
    """A positive count's place in words, as "eleventh", "twenty-first" or "one hundredth"."""
    if count < 1:
        msg = "a place is a positive count"
        raise ValueError(msg)
    words = spelled(count)
    cut = max(words.rfind(" "), words.rfind("-")) + 1
    last = words[cut:]
    if last in PLACE_WORDS:
        place = PLACE_WORDS[last]
    elif last.endswith("y"):
        place = f"{last[:-1]}ieth"
    else:
        place = f"{last}th"
    return words[:cut] + place


def spoken_date(day: date) -> str:
    """A day as a page says it: ``September 30``."""
    return f"{day.strftime('%B')} {day.day}"


def spoken_day(moment: str, zone: tzinfo) -> str:
    """A stored moment's day where the household is: ``September 30``. Text that is no moment,
    or a moment with no day there, is ``GradeValueUndecodable``."""
    try:
        day = datetime.fromisoformat(moment).astimezone(zone).date()
    except (ValueError, OverflowError) as error:
        raise GradeValueUndecodable from error
    return spoken_date(day)


def report_names(reports: tuple[ClassReport, ...], zone: tzinfo) -> dict[str, str]:
    """Each report as a sentence names it: "report added September 30", with its place among
    the reports added that day when it shares one, by acceptance order. A report an action made
    carries its capture's name and the day it was made current."""
    imported = [report for report in reports if report.acted_at is None]
    days = {report.report_id: spoken_day(report.imported_at, zone) for report in imported}
    names: dict[str, str] = {}
    of_capture: dict[str, str] = {}
    for report in imported:
        day = days[report.report_id]
        same_day = [other.report_id for other in imported if days[other.report_id] == day]
        place = same_day.index(report.report_id)
        named = f"{ordinal(place + 1)} " if place else ""
        names[report.report_id] = f"{named}report added {day}"
        of_capture[report.latest_of_capture] = names[report.report_id]
    for report in reports:
        if report.acted_at is not None:
            source = of_capture.get(
                report.latest_of_capture, f"report added {spoken_day(report.imported_at, zone)}"
            )
            made = spoken_day(report.acted_at, zone)
            names[report.report_id] = f"{source}, made current {made}"
    return names


def score_of(value: CurrentValue) -> str:
    """The school score: points out of the maximum, then the percent the school reported, each
    as written or in the review's words for its presence; one presence for the line when all
    three share a blank or uncaptured one."""
    points, most = value.cells["points"], value.cells["max_points"]
    average = value.cells["average"]
    alike = absent_alike(points, most, average)
    if alike is not None:
        return presence_words("Score", alike)
    return (
        f"School score: {described(*points, None)} / {described(*most, 'maximum')}"
        f" · {as_said(average, 'average')}"
    )


def score_line(value: CurrentValue, names: Mapping[str, str]) -> str:
    """A result's whole school score line: its score, then the report that supplied it, by the
    name ``report_names`` gives it in ``names``, when a newer report last showed the result."""
    line = score_of(value)
    source = supplied_by(value, names)
    return line if source is None else f"{line}, from the {source}"


def supplied_by(value: CurrentValue, names: Mapping[str, str]) -> str | None:
    """The name ``report_names`` gives in ``names`` to the report that supplied a result's value,
    when a newer report last showed the result; else None."""
    if value.last_shown is None or value.last_shown.report_id == value.report_id:
        return None
    return names.get(value.report_id, "report")
