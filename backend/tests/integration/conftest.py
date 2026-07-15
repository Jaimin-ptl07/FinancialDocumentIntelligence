"""Session-scoped containers for integration tests.

WINDOWS: TC_HOST is read at IMPORT time (it is a dataclass field default), so it
MUST be set before `testcontainers` is imported — setting it later silently does
nothing. See testcontainers-python#1009 and #407.
"""

from __future__ import annotations

import os
import sys
from collections.abc import Iterator
from urllib.parse import quote

if sys.platform == "win32":
    os.environ.setdefault("TC_HOST", "localhost")

import pytest
from minio import Minio
from testcontainers.minio import MinioContainer
from testcontainers.postgres import PostgresContainer
from testcontainers.rabbitmq import RabbitMqContainer
from testcontainers.redis import RedisContainer

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
