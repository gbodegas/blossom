# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 Gerardo Bodegas Martinez
"""A due date read under its school year's first month. The gradebook writes MM/DD with no
year, so the year comes from the year's label and the month it starts in, each time the date is
read. The confirmed month is kept on the school year alone (`grade_years`); no observation or
compared value holds it, and no resolved date is stored."""

from datetime import date

from blossom.grades.draft import MONTH_AND_DAY, SCHOOL_YEAR, Presence


def due_date_of(cell: tuple[Presence, str], year: str, first_month: int | None) -> date | None:
    """The day a reported MM/DD names in school year ``year``: in the label's first year from
    ``first_month`` on, else in its second. None, shown as written, without a confirmed month,
    for another presence, label or text, or for a day that year lacks."""
    presence, text = cell
    label = SCHOOL_YEAR.fullmatch(year)
    written = MONTH_AND_DAY.fullmatch(text)
    if presence is not Presence.REPORTED or first_month is None or not label or not written:
        return None
    first, second = int(label[1]), int(label[2])
    if second != first + 1 or first_month not in range(1, 13):
        return None
    month, day = int(written[1]), int(written[2])
    try:
        return date(first if month >= first_month else second, month, day)
    except ValueError:
        return None
