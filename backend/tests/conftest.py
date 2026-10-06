"""Test-wide fixtures.

Kept deliberately thin: fixtures needing Docker live in tests/integration/conftest.py
so the fast loop never imports testcontainers.
"""

import os
from collections.abc import Iterator

import pytest
import structlog

from fdi.platform.config import get_settings

# 32+ bytes: pyjwt warns on short HS256 keys (RFC 7518 s3.2), and the warning
# is correct -- production secrets are generated at least this long, so the
# test environment should not normalize anything shorter. ONE constant, not a
# literal per file: it already lives in 15+ env stubs and a drift would mean
# some suites signing with a key others reject. Here (not the integration
# conftest) so the unit suite can import it without testcontainers.
TEST_SIGNING_SECRET = "test-signing-secret-0123456789abcdef0123456789abcdef"


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


@pytest.fixture(autouse=True)
def _isolate_fdi_env(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """Hide the developer's real FDI_* environment from every test.

    Task 6 made Settings reject any unknown FDI_ var. That is correct for the app
    and hostile to the suite: a developer with an unrelated FDI_FOO=1 exported
    fails tests they did not touch, with a ValidationError naming Settings rather
    than their shell. Measured before this fixture: `FDI_FOO=1 pytest` -> 1 failed.
    FDI_WORKER_CONCURRENCY=9 separately broke `assert worker_concurrency == 2`.

    Clearing the whole PREFIX is the only fix that works. delenv over a known list
    cannot help -- the offending name is unknown by premise, which is the entire
    point of the validator catching it.

    Tests that want an FDI_ var set it themselves afterwards; monkeypatch applies
    in fixture order, so this runs first and they still win.
    """
    for key in [k for k in os.environ if k.upper().startswith("FDI_")]:
        monkeypatch.delenv(key, raising=False)
    get_settings.cache_clear()
    yield
    # monkeypatch restores the ENVIRONMENT, not the object built from it. Without
    # this, a Settings cached under a test's env is served to later test files.
    get_settings.cache_clear()
