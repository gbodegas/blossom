"""The system's notion of "now", as an injectable dependency.

Time is treated here the way any other external input is treated. The
application reads it through a seam, tests supply a fixed value, and the
default implementation is the only thing that calls the operating system.
"""

from datetime import UTC, date, datetime
from typing import Protocol


class Clock(Protocol):
    """Reads the current instant. Implementations must be safe to share across threads."""

    def now(self) -> datetime:
        """Return the current instant as a timezone-aware UTC datetime."""
        ...

    def today(self) -> date:
        """Return the current date."""
        ...


class SystemClock:
    """The real clock. The only implementation that touches the operating system."""

    def now(self) -> datetime:
        """Return the current instant in UTC."""
        return datetime.now(UTC)

    def today(self) -> date:
        """Return today's date in UTC."""
        return self.now().date()


class FrozenClock:
    """A clock pinned to one instant, for tests and for runs against fixture dates."""

    def __init__(self, instant: datetime) -> None:
        self._instant = instant

    def now(self) -> datetime:
        """Return the pinned instant."""
        return self._instant

    def today(self) -> date:
        """Return the pinned instant's date."""
        return self._instant.date()


def clock_from(pinned_date: date | None) -> Clock:
    """Return a frozen clock at ``pinned_date``, or the system clock when unset.

    ``pinned_date`` comes from ``BLOSSOM_TODAY``. The synthetic fixtures carry
    fixed August 2026 dates, so a run in any other week shows an empty page
    unless the clock is pinned.
    """
    if pinned_date is None:
        return SystemClock()
    return FrozenClock(datetime(pinned_date.year, pinned_date.month, pinned_date.day, tzinfo=UTC))
