"""The system's notion of "now" and of "today", as an injectable dependency.

Time is read through a seam, the way any other external input is: tests supply
a fixed value, and one implementation touches the operating system.

Two kinds of time are kept apart on purpose. An instant is always an aware UTC
datetime, because that is the only form that means the same thing everywhere.
A date is always the household's local date, because "due this week" is a
question about the days she lives in. Taking the date off a UTC instant is
wrong for most of an American evening: at 20:30 on a Wednesday in
``America/New_York`` the UTC date is already Thursday, so a week window
computed that way runs a day ahead of the household.

The zone is configuration with no default. No value is right for every family,
and a wrong one moves the school week without saying so, which is worse than
refusing to start. Windows ships no time zone database, so ``tzdata`` is a
dependency; without it ``ZoneInfo`` raises for every key.
"""

from datetime import UTC, date, datetime
from typing import Protocol
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from blossom.settings import TIMEZONE_VARIABLE


class TimeZoneUnavailable(RuntimeError):
    """Raised at startup when the household zone is missing or cannot be resolved."""


def household_zone(key: str | None) -> ZoneInfo:
    """Resolve the configured IANA key, or say precisely what is wrong with it."""
    if key is None or not key.strip():
        msg = (
            f"{TIMEZONE_VARIABLE} is not set. It takes an IANA key such as "
            f"America/New_York. There is no default because a wrong zone moves "
            f"the school week without saying so."
        )
        raise TimeZoneUnavailable(msg)
    try:
        return ZoneInfo(key.strip())
    except (ZoneInfoNotFoundError, ValueError) as error:
        msg = (
            f"{TIMEZONE_VARIABLE} is {key!r}, which cannot be resolved. It takes "
            f"an IANA key such as America/New_York, and on a system that ships no "
            f"time zone database every key fails until tzdata is installed."
        )
        raise TimeZoneUnavailable(msg) from error


class Clock(Protocol):
    """Reads the current instant and the household's date.

    Implementations must be safe to share across threads.
    """

    @property
    def zone(self) -> ZoneInfo:
        """The household's time zone, which is what makes ``today`` local."""
        ...

    def now(self) -> datetime:
        """Return the current instant as a timezone-aware UTC datetime."""
        ...

    def today(self) -> date:
        """Return the current date in the household's zone."""
        ...


class SystemClock:
    """The real clock. The only implementation that touches the operating system."""

    def __init__(self, zone: ZoneInfo) -> None:
        self._zone = zone

    @property
    def zone(self) -> ZoneInfo:
        """The household's time zone."""
        return self._zone

    def now(self) -> datetime:
        """Return the current instant in UTC."""
        return datetime.now(UTC)

    def today(self) -> date:
        """Return today's date where the household lives, not in UTC."""
        return self.now().astimezone(self._zone).date()


class FrozenClock:
    """A clock pinned to one instant, for tests and for runs against fixture dates."""

    def __init__(self, instant: datetime, zone: ZoneInfo) -> None:
        if instant.tzinfo is None:
            msg = "a frozen clock needs an aware instant; a naive one has no fixed meaning"
            raise ValueError(msg)
        self._instant = instant.astimezone(UTC)
        self._zone = zone

    @property
    def zone(self) -> ZoneInfo:
        """The household's time zone."""
        return self._zone

    def now(self) -> datetime:
        """Return the pinned instant, in UTC."""
        return self._instant

    def today(self) -> date:
        """Return the pinned instant's date in the household's zone."""
        return self._instant.astimezone(self._zone).date()


def clock_from(pinned_date: date | None, timezone_key: str | None) -> Clock:
    """Return a clock in the household's zone, frozen at ``pinned_date`` when given.

    ``pinned_date`` comes from ``BLOSSOM_TODAY`` and pins the clock to local
    midnight of that day, so a pinned run reads the date it names. The
    synthetic fixtures carry fixed August 2026 dates, so a run in any other
    week shows an empty page unless the clock is pinned.
    """
    zone = household_zone(timezone_key)
    if pinned_date is None:
        return SystemClock(zone)
    local_midnight = datetime(pinned_date.year, pinned_date.month, pinned_date.day, tzinfo=zone)
    return FrozenClock(local_midnight, zone)
