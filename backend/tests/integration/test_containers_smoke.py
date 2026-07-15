"""Proves all four containers start and are reachable FROM THIS HOST.

If this fails on Windows, see testcontainers-python#1015 (open regression
against 4.14.2) before building anything else on these fixtures.
"""

from __future__ import annotations

import pytest
import sqlalchemy
from minio import Minio
from sqlalchemy.ext.asyncio import create_async_engine

pytestmark = pytest.mark.integration


async def test_postgres_reachable(postgres_url: str) -> None:
    assert postgres_url.startswith("postgresql+asyncpg://")
    engine = create_async_engine(postgres_url)
    try:
        async with engine.connect() as conn:
            result = await conn.execute(sqlalchemy.text("SELECT 1"))
            assert result.scalar_one() == 1
    finally:
        await engine.dispose()


def test_minio_reachable(minio_client: Minio) -> None:
    minio_client.make_bucket("smoke")
    assert minio_client.bucket_exists("smoke")


def test_amqp_url_well_formed(amqp_url: str) -> None:
    assert amqp_url.startswith("amqp://")
    assert amqp_url.endswith("/%2F"), "default vhost must be percent-encoded"


def test_redis_url_well_formed(redis_url: str) -> None:
    assert redis_url.startswith("redis://")
    assert redis_url.endswith("/0")
