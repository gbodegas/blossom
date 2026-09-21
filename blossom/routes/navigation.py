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

Target = Literal["week", "today", "family", "to_turn_in"]
TARGETS: Final[tuple[str, ...]] = ("week", "today", "family", "to_turn_in")
TO_TURN_IN_PAGE: Final = "/student/to-turn-in"
"""Her list of everything she reports as still to turn in, on a page of its own."""
TO_TURN_IN: Final = "to-turn-in"
"""The id the list has on that page and on her week, there whether or not it has rows."""
RETURN_FIELDS: Final = frozenset({"return_to", "week", "plan_id"})
"""The three fields that say where a reader came from, on a link and on a form alike."""
PLAN_ID_MAX_LENGTH: Final = 200
"""Longer than any draft id the graph makes; a longer one is not looked up."""


def segment(value: str) -> str:
    """One path segment, escaped: a slash, a question mark, a space, or anything else that
    would read as part of the address's shape is written as its bytes.

    The server undoes the escaping before it matches a route, so an escaped
    slash arrives as a slash. The routes that carry an assignment's id take
    it as the rest of the path up to their own ending, and so receive an id
    that holds one as the one value it is."""
    return "".join(
        character
        if character in UNRESERVED
        else "".join(f"%{byte:02X}" for byte in character.encode("utf-8"))
        for character in value
    )


def assignment_anchor(assignment_id: str) -> str:
    """One DOM id, shared by an assignment and every link to it."""
    return f"assignment-{segment(assignment_id)}"


NOTES_PAGE: Final = "/student/homework-notes"
NEW_NOTE_PAGE: Final = "/student/homework-notes/new"
ARCHIVED_NOTES_PAGE: Final = "/student/homework-notes/archived"
NOTE_ACTIONS: Final = "/student/actions/homework-notes"
NOTES: Final = "homework-notes"
"""The id of the place that holds her homework notes, on her week and on their own page."""
NOTE_RESULT: Final = "note-result"
"""The id of the place on a note's page that says what a save did, which a redirect lands on."""


def address(path: str, fragment: str = "", **query: str | None) -> str:
    """An address on this site from a path, the query values that are not blank, and a
    fragment, each escaped by the framework."""
    given = {name: value for name, value in query.items() if value}
    made = URL(path).include_query_params(**given) if given else URL(path)
    return str(made.replace(fragment=fragment) if fragment else made)


def details_href(assignment_id: str, *, fragment: str = "", **context: str | None) -> str:
    """The address of one assignment's details, with where the reader came from.

    ``context`` is the navigation data the details carry back: which page,
    which week, which plan, and what the page is to show. A value that is
    ``None`` or blank is left out. ``fragment`` is a place on the page to
    land on, the result of a save for one.
    """
    given = {name: value for name, value in context.items() if value}
    made = URL(f"{DETAILS}/{segment(assignment_id)}")
    made = made.include_query_params(**given) if given else made
    return str(made.replace(fragment=fragment) if fragment else made)


TODAYS_PLAN: Final = "todays-plan"
"""The id of the place on her week that holds today's plan, whichever plan that is. A saved
plan keeps an id of its own, made from its draft, which the family page and its history
link to; her side links to this place instead, so a link written under one plan still
lands when another has taken its place, and on a day with no plan the place says so."""


def todays_plan_href() -> str:
    """The address of today's plan on her week: the place, and the word that the plan was
    asked for, so the place is there to land on even when no plan is."""
    return address(WEEK_PAGE, fragment=TODAYS_PLAN, show_plan="1")


def note_href(capture_id: str, *, fragment: str = "", **query: str | None) -> str:
    """The address of one homework note's page. A note's id is a UUID, which needs no
    escaping, and goes through the same escaping as any other id all the same."""
    return address(f"{NOTES_PAGE}/{segment(capture_id)}", fragment, **query)


def note_help_href(capture_id: str) -> str:
    """The page that offers to ask for help about one note. Opening it sends nothing."""
    return f"{NOTES_PAGE}/{segment(capture_id)}/help"


def note_action(capture_id: str, step: str) -> str:
    """The route one change to a note goes through: ``edit``, ``archive``, ``restore``, or
    ``ask-for-help``."""
    return f"{NOTE_ACTIONS}/{segment(capture_id)}/{step}"


def note_anchor(capture_id: str) -> str:
    """One DOM id, shared by a note's row and every link to it."""
    return f"note-{segment(capture_id)}"


def result_anchor(assignment_id: str) -> str:
    """The id of the place on a page that says what a save or an undo did to one
    assignment's update, which a redirect lands on. The page writes the id with this and
    the redirect names it with this, so the two agree whatever the assignment's id holds."""
    return f"update-result-{segment(assignment_id)}"


def hand_in_result_anchor(assignment_id: str) -> str:
    """The same for her hand-in update on an assignment's details."""
    return f"hand-in-result-{segment(assignment_id)}"


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
    by her own signed-in device is not hers to open, and no page of hers
    writes a link or a form that names it, so it is not valid either: a
    link becomes her safe default without a word, as any other link she
    cannot follow would, and a form that names it is refused like any other
    form these pages did not make.
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
        return fallback, False
    return ReturnTo(cast(Target, target), week=chosen, plan_id=plan_id or None), True


def week_href(week: date | None, assignment_id: str, **more: str | None) -> str:
    """Her week with one card in view, the fold around it open. The fragment is the
    card's own id, which the page writes with the same helper, so the address names that
    card as written and no other, whatever the assignment's id holds."""
    return address(
        WEEK_PAGE,
        fragment=assignment_anchor(assignment_id),
        week=None if week is None else week.isoformat(),
        **more,
    )
