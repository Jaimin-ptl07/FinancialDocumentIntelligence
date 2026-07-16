"""Test-wide fixtures.

Kept deliberately thin: fixtures needing Docker live in tests/integration/conftest.py
so the fast loop never imports testcontainers.
"""

from collections.abc import Iterator

import pytest
import structlog


@pytest.fixture(autouse=True)
def _isolate_contextvars() -> Iterator[None]:
    """Give every test an empty structlog context.

    structlog's context lives in contextvars, which pytest does NOT reset between
    tests -- they share one thread and one Context. Verified: a correlation_id
    bound in test_logging.py is still readable from a different test FILE.

    Nothing depends on the leak today (every test that reads an ID binds it
    first), so this buys isolation rather than fixing a live bug. It is here
    because the failure it prevents is the expensive kind: a test asserting an ID
    is ABSENT, or has a specific value, would pass or fail on which tests ran
    before it -- and would be debugged as a logging bug, not a fixture bug.

    It also matches production, where each Celery task binds a fresh ID from its
    message header before asyncio.run(). A context inherited from unrelated
    earlier work models nothing real.
    """
    structlog.contextvars.clear_contextvars()
    yield
    structlog.contextvars.clear_contextvars()
