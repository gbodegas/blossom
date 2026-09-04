"""Shared test helpers.

`record` lives here rather than in one test module so the modules that need a
`SourceRecord` do not import each other; cross-imports between test files make
the suite's collection order matter, which it should not.

This is a plain module rather than `conftest.py`: importing from a conftest
makes the same file reachable under two module names, which mypy rejects.
"""

from datetime import UTC, datetime
from zoneinfo import ZoneInfo

from langchain_core.callbacks.manager import CallbackManager
from langchain_core.tracers.langchain import LangChainTracer

from blossom.clock import FrozenClock
from blossom.reconciliation import SourceChannel, SourceRecord
from blossom.settings import TIMEZONE_VARIABLE, Settings

FIXTURE_TIMEZONE = "America/New_York"
"""The zone the synthetic fixtures are written in. A fictional household's."""

OBSERVED_AT = datetime(2026, 8, 19, 9, 0, tzinfo=UTC)


def fixture_clock(instant: datetime | None = None) -> FrozenClock:
    """A clock in the fixtures' zone, pinned to their week unless told otherwise."""
    return FrozenClock(instant or OBSERVED_AT, ZoneInfo(FIXTURE_TIMEZONE))


def fixture_settings(**environ: str) -> Settings:
    """Settings for a test that starts the app, in the fixtures' time zone.

    The application has no default zone, so a test that builds settings from an
    explicit mapping has to supply one. This keeps that from being repeated,
    and keeps the value in one place if the fixtures ever move.
    """
    return Settings.from_environment({TIMEZONE_VARIABLE: FIXTURE_TIMEZONE, **environ})


def record(channel: SourceChannel, value: str, *, confidence: float = 0.8) -> SourceRecord:
    """Build a source record with a fixed observation time."""
    return SourceRecord(
        channel=channel,
        asserted_value=value,
        observed_at=OBSERVED_AT,
        confidence=confidence,
    )


def hosted_tracer_attached() -> bool:
    """Ask the framework's real consumer whether it would attach a hosted tracer."""
    handlers = CallbackManager.configure().handlers
    return any(isinstance(handler, LangChainTracer) for handler in handlers)
