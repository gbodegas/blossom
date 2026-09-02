"""The three principals the system serves.

The student is the primary user, a parent reviews and corrects, and the
verifier is a checking layer rather than a person. Their interests conflict,
so they are distinct values rather than a role flag on one user record, which
would hide that conflict.

This enum grants nothing. It names who a request is for. What each principal
may see is defined by the view models in ``blossom/views.py`` and the route
trees under ``blossom/routes/``.
"""

from enum import StrEnum


class Principal(StrEnum):
    """Who a request is for. Names a principal; grants nothing."""

    STUDENT = "STUDENT"
    PARENT = "PARENT"
    VERIFIER = "VERIFIER"
