"""Session-wide guarantees for the test run itself.

Tests that drive the model framework directly run outside the application
lifespan, so nothing in the code under test forces hosted tracing off for
them. A shell with ``LANGSMITH_TRACING=true`` and a key would attach the hosted
tracer to those runs and ship a fixture student's data. The session fixture
below closes that path before the first test and checks after the last that
nothing along the way attached a hosted tracer.

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
    """Force hosted tracing off for the whole session and confirm it stayed off.

    The langsmith pytest plugin loads in every run of this suite. With test
    tracking off its helpers are no-ops and nothing is uploaded, so the
    variable is set here as well, whether or not any test uses the plugin.
    """
    enforce_local_only_tracing()
    os.environ["LANGSMITH_TEST_TRACKING"] = "false"
    assert not hosted_tracer_attached(), "a hosted tracer was attached before the suite began"
    yield
    assert not hosted_tracer_attached(), "a test left a hosted tracer attached"
