"""Addresses the pages make for one another, built from values and never from a URL handed in.

A link to an assignment's details names the assignment by id in the path and
says, as data, where the reader came from, so the details can offer the way
back. Every value is escaped where the address is made: an id goes into the
path one escaped segment at a time, and the rest go through the framework's
own query encoding. No address here is read from a request and sent back, so
nothing a visitor types can become a place a page sends them.
"""

from typing import Final

from starlette.datastructures import URL

DETAILS: Final = "/student/assignments"
UNRESERVED: Final = frozenset("ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-._~")
"""The characters a path segment may carry as they are; every other one is escaped."""


def segment(value: str) -> str:
    """One path segment, escaped: a slash, a question mark, a space, or anything else that
    would read as part of the address's shape is written as its bytes."""
    return "".join(
        character
        if character in UNRESERVED
        else "".join(f"%{byte:02X}" for byte in character.encode("utf-8"))
        for character in value
    )


def details_href(assignment_id: str, **context: str | None) -> str:
    """The address of one assignment's details, with where the reader came from.

    ``context`` is the navigation data the details carry back: which page,
    which week, which plan. A value that is ``None`` or blank is left out.
    """
    given = {name: value for name, value in context.items() if value}
    return str(URL(f"{DETAILS}/{segment(assignment_id)}").include_query_params(**given))
