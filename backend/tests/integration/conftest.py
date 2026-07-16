"""Session-scoped containers for integration tests.

WINDOWS: TC_HOST is read at IMPORT time (it is a dataclass field default), so it
MUST be set before `testcontainers` is imported — setting it later silently does
nothing. See testcontainers-python#1009 and #407.
"""

from __future__ import annotations

import os
import sys
from collections.abc import AsyncIterator, Iterator
from pathlib import Path
from urllib.parse import quote

if sys.platform == "win32":
    os.environ.setdefault("TC_HOST", "localhost")

import pytest
from alembic import command
from alembic.config import Config
from minio import Minio
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine
from testcontainers.minio import MinioContainer
from testcontainers.postgres import PostgresContainer
from testcontainers.rabbitmq import RabbitMqContainer
from testcontainers.redis import RedisContainer

BACKEND_DIR = Path(__file__).resolve().parents[2]

PG_IMAGE = "postgres:18.4"
# NOT RELEASE.2025-10-15T17-29-55Z — that release exists but was never published
# as a container image (MinIO stopped publishing free images ~2025-10-23).
MINIO_IMAGE = "minio/minio:RELEASE.2025-09-07T16-13-09Z"
RABBITMQ_IMAGE = "rabbitmq:4.3.2-management"
REDIS_IMAGE = "redis:8.8.0"


@pytest.fixture(scope="session")
def postgres_url() -> Iterator[str]:
    """postgresql+asyncpg://test:test@host:port/test

    The method-level `driver` overrides the constructor default (psycopg2).
    Readiness runs `psql` INSIDE the container, so no Python driver is needed
    to wait — `driver="asyncpg"` cannot break the wait strategy.
    """
    with PostgresContainer(PG_IMAGE) as pg:
        yield pg.get_connection_url(driver="asyncpg")


@pytest.fixture(scope="session")
def minio_container() -> Iterator[MinioContainer]:
    with MinioContainer(MINIO_IMAGE) as mc:
        yield mc


@pytest.fixture(scope="session")
def minio_client(minio_container: MinioContainer) -> Minio:
    return minio_container.get_client()


@pytest.fixture(scope="session")
def minio_endpoint(minio_container: MinioContainer) -> str:
    """host:port, no scheme."""
    endpoint: str = minio_container.get_config()["endpoint"]
    return endpoint


@pytest.fixture(scope="session")
def amqp_url() -> Iterator[str]:
    """amqp://guest:guest@host:port/%2F

    RabbitMqContainer has NO url builder — assemble it. The default vhost '/'
    MUST be percent-encoded: the RabbitMQ URI spec says `vhost = segment`, so
    the common `...//` form is out of spec.
    """
    with RabbitMqContainer(RABBITMQ_IMAGE) as rabbit:
        p = rabbit.get_connection_params()
        user = quote(p.credentials.username, safe="")
        pwd = quote(p.credentials.password, safe="")
        vhost = quote(p.virtual_host, safe="")
        yield f"amqp://{user}:{pwd}@{p.host}:{p.port}/{vhost}"


@pytest.fixture(scope="session")
def redis_url() -> Iterator[str]:
    """redis://host:port/0 — RedisContainer has no get_connection_url()."""
    with RedisContainer(REDIS_IMAGE) as rc:
        host = rc.get_container_host_ip()
        port = rc.get_exposed_port(rc.port)
        yield f"redis://{host}:{port}/0"


# Settings has six required fields and `alembic/env.py` builds one at import, so
# all six must exist even though a migration only ever uses database_url.
_SETTINGS_STUBS = {
    "FDI_BROKER_URL": "amqp://guest:guest@localhost:5672/%2F",
    "FDI_RESULT_BACKEND": "redis://localhost:6379/0",
    "FDI_S3_ENDPOINT": "localhost:9000",
    "FDI_S3_ACCESS_KEY": "minioadmin",
    "FDI_S3_SECRET_KEY": "minioadmin",
}


@pytest.fixture(scope="session")
def _alembic_applied(postgres_url: str) -> None:
    """Run migrations once per session against the container.

    Sync on purpose: `command.upgrade` calls `asyncio.run()` inside env.py, which
    would raise from within an already-running loop. pytest resolves sync
    fixtures outside the test's event loop, so this is the safe place for it.

    The env vars exist ONLY for the duration of the upgrade and are undone before
    this fixture returns -- deliberately not via `yield` + teardown. A
    session-scoped teardown runs after the LAST test, so a fixture that held them
    open would leave every unit test running with FDI_DATABASE_URL set and a
    `get_settings` cache pinned to a container that is about to die. Collection
    order makes that concrete: tests/integration runs before tests/unit, and
    tests/unit/test_config.py asserts that `Settings()` FAILS when required vars
    are absent. It survives today only because it monkeypatches every key it
    reads -- i.e. by luck, not design. Nothing after the upgrade needs the env:
    `migrated_engine` takes `postgres_url` directly.
    """
    monkeypatch = pytest.MonkeyPatch()
    from fdi.platform.config import get_settings

    try:
        monkeypatch.setenv("FDI_DATABASE_URL", postgres_url)
        for key, value in _SETTINGS_STUBS.items():
            if key not in os.environ:
                monkeypatch.setenv(key, value)
        get_settings.cache_clear()
        # No script_location override: `%(here)s/alembic` in the ini must resolve
        # on its own, or this fixture would hide a broken ini from the suite.
        command.upgrade(Config(str(BACKEND_DIR / "alembic.ini")), "head")
    finally:
        monkeypatch.undo()
        get_settings.cache_clear()


@pytest.fixture
async def migrated_engine(
    postgres_url: str, _alembic_applied: None
) -> AsyncIterator[AsyncEngine]:
    """Function-scoped ON PURPOSE - do NOT make this session-scoped.

    pytest-asyncio 1.x runs each test on its own function-scoped event loop
    (asyncio_default_fixture_loop_scope defaults to None). A session-scoped
    async fixture would bind its pool to a different loop than the test using
    it, producing exactly the failure this project's spec warns about:
    `got Future attached to a different loop`.

    Function scope also mirrors production: every Celery task builds an engine
    inside its own asyncio.run() and disposes it in `finally`. The test should
    look like the thing it tests. The container stays session-scoped, so the
    cost is a connection handshake per test, not a container start.
    """
    engine = create_async_engine(postgres_url)
    yield engine
    await engine.dispose()
