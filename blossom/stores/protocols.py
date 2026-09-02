"""The shape every long-term store shares.

``name`` is what a retrieval result reports as its origin, so a fact can always
be traced back to the store it came from. ``retention_policy`` is prose rather
than a duration because the three stores keep their contents for different
reasons, and the policy has to state the reason, not just the schedule.

No function accepts a ``Store``; the three stores define both attributes, but
nothing type-checks against this protocol.
"""

from typing import Protocol


class Store(Protocol):
    """What every long-term store exposes: an identity and a retention rule."""

    retention_policy: str
    name: str
