"""intake_document against real Postgres + MinIO.

Mirrors sibling fixtures: session_factory from migrated_engine (test_document_
repository.py), store from minio_client + ensure_bucket() (test_artifact_
store.py). The migrated DB and MinIO bucket are session-scoped, so each test
mints its own doc_key to avoid cross-test version/checksum collisions.
"""

import uuid

import pytest
from minio import Minio
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from fdi.pipeline.intake import document_key, intake_document
from fdi.platform.db.engine import make_session_factory
from fdi.platform.storage.artifact_store import ArtifactStore

pytestmark = pytest.mark.integration


@pytest.fixture
def session_factory(migrated_engine: AsyncEngine) -> async_sessionmaker[AsyncSession]:
    return make_session_factory(migrated_engine)


@pytest.fixture
async def store(minio_client: Minio) -> ArtifactStore:
    s = ArtifactStore(minio_client, bucket="test-artifacts")
    await s.ensure_bucket()
    return s


async def test_intake_stores_dedups_and_versions(
    session_factory: async_sessionmaker[AsyncSession], store: ArtifactStore
) -> None:
    key = f"doc-{uuid.uuid4()}"
    a = await intake_document(
        pdf=b"PDF-A",
        filename="a.pdf",
        doc_key=key,
        content_type="application/pdf",
        session_factory=session_factory,
        store=store,
    )
    assert a.version == 1 and a.checksum is not None
    assert await store.get_bytes(document_key(a.checksum)) == b"PDF-A"

    # same bytes -> dedup -> same id, still version 1
    a2 = await intake_document(
        pdf=b"PDF-A",
        filename="a-again.pdf",
        doc_key=key,
        content_type="application/pdf",
        session_factory=session_factory,
        store=store,
    )
    assert a2.id == a.id and a2.version == 1

    # different bytes, same doc_key -> version 2
    b = await intake_document(
        pdf=b"PDF-B",
        filename="b.pdf",
        doc_key=key,
        content_type="application/pdf",
        session_factory=session_factory,
        store=store,
    )
    assert b.version == 2 and b.id != a.id
