"""The shape every long-term store shares.

Two attributes, both deliberate. ``name`` is what a retrieval result reports as
its origin, so a fact can always be traced back to the store it came from.
``retention_policy`` is prose rather than a duration because the three stores
keep their contents for different reasons, and a number would record the
schedule while losing the justification.

Known gap: nothing type-checks against this protocol yet. The three stores each
define both attributes, but no function currently accepts a ``Store``, so the
protocol documents an intention rather than enforcing one.
"""

from typing import Protocol


class Store(Protocol):
    """What every long-term store exposes: an identity and a retention rule."""

    retention_policy: str
    name: str
