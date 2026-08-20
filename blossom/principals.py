"""The three principals the system serves, as an enum rather than a role flag.

They are separate here because their interests genuinely conflict. The student
is the primary user; a parent is a collaborator who reviews and corrects; the
verifier is a checking layer rather than a person. Collapsing them into a role
field on one user record is the easier build, and it makes the conflict
invisible at exactly the point where it should be explicit.

Note that this enum does not grant anything. It names who a request is for. The
projection each principal is allowed to see is a property of the separate view
models in ``blossom/views.py`` and the separate route trees under
``blossom/routes/``.
"""

from enum import StrEnum


class Principal(StrEnum):
    """Who a request is for. Names a principal; grants nothing."""

    STUDENT = "STUDENT"
    PARENT = "PARENT"
    VERIFIER = "VERIFIER"
