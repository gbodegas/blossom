"""Addresses the pages make for one another, built from values and never from a URL handed in.

A link to an assignment's details names the assignment by id in the path and
says, as data, where the reader came from, so the details can offer the way
back: her week, today's plan, or the family page. The way back is three small
fields, read and checked here, and an address is made from them only on the
server. No address is ever read from a request and sent back, no ``next``
and no Referer, so nothing a visitor types can become a place a page sends
them; a value that is not one of the few this module knows is refused on a
form and ignored on a link.

Every value is escaped where the address is made: an id goes into the path as
one escaped segment, and the rest go through the framework's own query
encoding. What she typed, a status or a note, never goes into an address.
"""

from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import date
from typing import Final, Literal, cast

from starlette.datastructures import URL

DETAILS: Final = "/student/assignments"
WEEK_PAGE: Final = "/student/due-this-week"
FAMILY_PAGE: Final = "/parent"
UNRESERVED: Final = frozenset("ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-._~")
"""The characters a path segment may carry as they are; every other one is escaped."""

Target = Literal["week", "today", "family"]
TARGETS: Final[tuple[str, ...]] = ("week", "today", "family")
RETURN_FIELDS: Final = frozenset({"return_to", "week", "plan_id"})
"""The three fields that say where a reader came from, on a link and on a form alike."""
PLAN_ID_MAX_LENGTH: Final = 200
"""Longer than any draft id the graph makes; a longer one is not looked up."""


def segment(value: str) -> str:
    """One path segment, escaped: a slash, a question mark, a space, or anything else that
    would read as part of the address's shape is written as its bytes."""
    return "".join(
        character
        if character in UNRESERVED
        else "".join(f"%{byte:02X}" for byte in character.encode("utf-8"))
        for character in value
    )


def address(path: str, fragment: str = "", **query: str | None) -> str:
    """An address on this site from a path, the query values that are not blank, and a
    fragment, each escaped by the framework."""
    given = {name: value for name, value in query.items() if value}
    made = URL(path).include_query_params(**given) if given else URL(path)
    return str(made.replace(fragment=fragment) if fragment else made)


def details_href(assignment_id: str, **context: str | None) -> str:
    """The address of one assignment's details, with where the reader came from.

    ``context`` is the navigation data the details carry back: which page,
    which week, which plan, and what the page is to show. A value that is
    ``None`` or blank is left out.
    """
    given = {name: value for name, value in context.items() if value}
    path = f"{DETAILS}/{segment(assignment_id)}"
    return str(URL(path).include_query_params(**given)) if given else path


@dataclass(frozen=True)
class ReturnTo:
    """Where an assignment's details send their reader back to, as data."""

    target: Target
    week: date | None = None
    """With ``week``: any day of the school week she was looking at; her current week when
    ``None``. Never the day of a report."""
    plan_id: str | None = None
    """With ``family``: the plan she or a parent was reading; the assignment updates when
    ``None``."""

    def fields(self) -> dict[str, str]:
        """The three fields as a link or a form carries them, blank where there is none."""
        return {
            "return_to": self.target,
            "week": "" if self.week is None else self.week.isoformat(),
            "plan_id": self.plan_id or "",
        }


def safe_default(viewer: str) -> ReturnTo:
    """Where a reader goes back to when nothing valid says otherwise: the family page for a
    parent who is signed in, her current week for her and for an open household."""
    return ReturnTo("family") if viewer == "parent" else ReturnTo("week")


def read_return(
    given: Mapping[str, str], *, viewer: str, showable: Callable[[date], bool]
) -> tuple[ReturnTo, bool]:
    """The way back the fields name, and whether they named one this site makes.

    Nothing named is the reader's safe default, and valid. A page that is
    not one of the three, a week that is not a day the week page can show or
    that comes with another page, and a plan that comes with anything but
    the family page or is too long to be one, are not valid: a link falls
    back to the safe default, and a form is refused. A family page asked for
    by her own signed-in device is not hers to open, so it becomes her safe
    default without a word, as any other link she cannot follow would.
    """
    target = given.get("return_to", "").strip()
    week = given.get("week", "").strip()
    plan_id = given.get("plan_id", "").strip()
    fallback = safe_default(viewer)
    if not target:
        return fallback, not (week or plan_id)
    if target not in TARGETS:
        return fallback, False
    if (week and target != "week") or (plan_id and target != "family"):
        return fallback, False
    if len(plan_id) > PLAN_ID_MAX_LENGTH:
        return fallback, False
    chosen: date | None = None
    if week:
        try:
            chosen = date.fromisoformat(week)
        except ValueError:
            return fallback, False
        if not showable(chosen):
            return fallback, False
    if target == "family" and viewer == "student":
        return fallback, True
    return ReturnTo(cast(Target, target), week=chosen, plan_id=plan_id or None), True


def week_href(week: date | None, assignment_id: str, **more: str | None) -> str:
    """Her week with one card in view, the fold around it open."""
    return address(
        WEEK_PAGE,
        fragment=f"assignment-{assignment_id}",
        week=None if week is None else week.isoformat(),
        **more,
    )
