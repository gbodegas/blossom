"""Shared test helpers.

`record` lives here rather than in one test module so the modules that need a
`SourceRecord` do not import each other; cross-imports between test files make
the suite's collection order matter, which it should not.

Deliberately not `conftest.py`. Importing from a conftest works but makes the
same file reachable under two module names, which mypy rejects outright.
"""

from datetime import UTC, datetime

from blossom.reconciliation import SourceChannel, SourceRecord

OBSERVED_AT = datetime(2026, 8, 19, 9, 0, tzinfo=UTC)


def record(channel: SourceChannel, value: str, *, confidence: float = 0.8) -> SourceRecord:
    """Build a source record with a fixed observation time."""
    return SourceRecord(
        channel=channel,
        asserted_value=value,
        observed_at=OBSERVED_AT,
        confidence=confidence,
    )
