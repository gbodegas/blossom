"""Session-wide guarantees for the test run itself.

Tests that drive the model framework directly run outside the application
lifespan, so nothing in the code under test forces hosted tracing off for
them. A shell with ``LANGSMITH_TRACING=true`` and a key would attach the hosted
tracer to those runs and ship a fixture student's data. The session fixture
below closes that path before the first test; the function fixture checks after
every test that no hosted tracer is attached, so a leak is pinned to the test
that caused it rather than to whichever test ran last.

Only fixtures live here. Helpers are in ``tests/support.py``: importing from a
conftest makes the same file reachable under two module names, which mypy
rejects.
"""

import os
from collections.abc import Iterator

import pytest

from blossom.settings import enforce_local_only_tracing
from tests.support import hosted_tracer_attached


@pytest.fixture(scope="session", autouse=True)
def local_only_tracing() -> Iterator[None]:
    """Force hosted tracing off for the whole session.

    The langsmith pytest plugin loads in every run of this suite. With test
    tracking off its helpers are no-ops and nothing is uploaded, so the
    variable is set here as well, whether or not any test uses the plugin.
    """
    enforce_local_only_tracing()
    os.environ["LANGSMITH_TEST_TRACKING"] = "false"
    assert not hosted_tracer_attached(), "a hosted tracer was attached before the suite began"
    yield


@pytest.fixture(autouse=True)
def no_hosted_tracer_left_behind() -> Iterator[None]:
    """After each test, nothing along the way attached a hosted tracer."""
    yield
    assert not hosted_tracer_attached(), "this test left a hosted tracer attached"
