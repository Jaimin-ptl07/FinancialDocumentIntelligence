"""GET /documents in its two modes.

With `?doc_key=` it is the version list it has always been (backward
compatibility is the point -- SP-17's /extract/document flow and the
existing frontend both call it that way). Without, it is the corpus list a
UI needs: latest version per doc_key, each with a {status: count} summary
over the LATEST run per stage name, paginated.
"""

import uuid
from contextlib import asynccontextmanager

import pytest
from httpx import ASGITransport, AsyncClient

from fdi.platform.config import get_settings
from tests.integration.conftest import authenticate

pytestmark = pytest.mark.integration


@pytest.fixture
async def make_client(postgres_url, minio_endpoint, amqp_url, redis_url, monkeypatch, invitee):
    @asynccontextmanager
    async def _make():
        env = {
            "FDI_DATABASE_URL": postgres_url,
            "FDI_BROKER_URL": amqp_url,
            "FDI_RESULT_BACKEND": redis_url,
            "FDI_S3_ENDPOINT": minio_endpoint,
            "FDI_S3_ACCESS_KEY": "minioadmin",
            "FDI_S3_SECRET_KEY": "minioadmin",
            "FDI_AUTH_SECRET": "test-signing-secret-0123456789abcdef0123456789abcdef",
        }
        for key, value in env.items():
            monkeypatch.setenv(key, value)
        get_settings.cache_clear()

        from fdi.api.app import create_app

        app = create_app()
        async with app.router.lifespan_context(app):
            transport = ASGITransport(app=app, raise_app_exceptions=True)
            async with AsyncClient(transport=transport, base_url="http://test") as client:
                await authenticate(client, invitee)
                yield client

    return _make


async def _seed(session_factory, doc_key: str, versions: int) -> list[str]:
    """`versions` document rows sharing one doc_key, versions 1..n."""
    from fdi.platform.db.repository import DocumentRepository

    ids: list[str] = []
    async with session_factory() as s:
        repo = DocumentRepository(s)
        for v in range(1, versions + 1):
            doc_id = str(uuid.uuid4())
            await repo.create(
                id=doc_id, checksum=uuid.uuid4().hex, filename=f"{doc_key}-v{v}.pdf",
                byte_size=10, content_type="application/pdf", doc_key=doc_key, version=v,
            )
            ids.append(doc_id)
        await s.commit()
    return ids


async def _seed_runs(session_factory, doc_id: str) -> None:
    """ocr: two runs (failed then done -- only `done` must be counted),
    normalize: one running run."""
    from fdi.platform.db.repository import RegistryRepository

    async with session_factory() as s:
        repo = RegistryRepository(s)
        first = str(uuid.uuid4())
        await repo.start_run(first, doc_id, "ocr", 1, "h", None)
        await repo.fail_run(first, "boom")
        await s.commit()
    async with session_factory() as s:
        repo = RegistryRepository(s)
        second = str(uuid.uuid4())
        await repo.start_run(second, doc_id, "ocr", 1, "h", None)
        await repo.finish_run(second, "oh", "artifacts/ocr.json")
        await repo.start_run(str(uuid.uuid4()), doc_id, "normalize", 1, "h", None)
        await s.commit()


@pytest.fixture
def session_factory(migrated_engine):
    from fdi.platform.db.engine import make_session_factory

    return make_session_factory(migrated_engine)


async def test_list_documents_with_doc_key_is_unchanged(make_client, session_factory) -> None:
    key = f"acme-{uuid.uuid4().hex}"
    await _seed(session_factory, key, versions=2)
    async with make_client() as client:
        r = await client.get("/documents", params={"doc_key": key})
    assert r.status_code == 200
    body = r.json()
    assert [v["version"] for v in body["versions"]] == [1, 2]
    assert "documents" not in body


async def test_list_all_documents_returns_latest_version_per_doc_key(
    make_client, session_factory
) -> None:
    key = f"acme-{uuid.uuid4().hex}"
    await _seed(session_factory, key, versions=3)
    async with make_client() as client:
        r = await client.get("/documents", params={"limit": 200})
    assert r.status_code == 200
    body = r.json()
    mine = [d for d in body["documents"] if d["doc_key"] == key]
    assert len(mine) == 1
    assert mine[0]["version"] == 3
    assert mine[0]["filename"] == f"{key}-v3.pdf"


async def test_list_all_documents_carries_latest_run_stage_counts(
    make_client, session_factory
) -> None:
    key = f"acme-{uuid.uuid4().hex}"
    [doc_id] = await _seed(session_factory, key, versions=1)
    await _seed_runs(session_factory, doc_id)
    async with make_client() as client:
        r = await client.get("/documents", params={"limit": 200})
    mine = [d for d in r.json()["documents"] if d["doc_key"] == key]
    # ocr's LATEST run is done (the earlier failed one must not be counted).
    assert mine[0]["stage_counts"] == {"done": 1, "running": 1}


async def test_list_all_documents_paginates(make_client, session_factory) -> None:
    prefix = uuid.uuid4().hex
    for i in range(3):
        await _seed(session_factory, f"{prefix}-{i}", versions=1)
    async with make_client() as client:
        first = (await client.get("/documents", params={"limit": 1, "offset": 0})).json()
        second = (await client.get("/documents", params={"limit": 1, "offset": 1})).json()
    assert len(first["documents"]) == 1
    assert len(second["documents"]) == 1
    assert first["documents"][0]["id"] != second["documents"][0]["id"]
    assert first["limit"] == 1 and first["offset"] == 0
    assert second["offset"] == 1
    assert first["total"] == second["total"] >= 3


async def test_list_all_documents_rejects_a_bad_limit(make_client) -> None:
    async with make_client() as client:
        r = await client.get("/documents", params={"limit": 0})
    assert r.status_code == 422
