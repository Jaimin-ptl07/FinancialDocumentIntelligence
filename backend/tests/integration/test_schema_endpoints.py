"""GET/PUT /schema and POST /schema/validate.

Writes persist to a MinIO object, NOT back to the repo file: the Dockerfile
COPYs docs/architecture/field-extraction-schema.json into the image at build
time (backend/Dockerfile:39), so a write inside the container mutates a
layer-owned file that vanishes on the next `up` -- or, worse, succeeds and
leaves each replica with a different schema that no `git status` shows. The
on-disk file stays the immutable v3.1.0 seed, read only when the object is
absent.
"""

import uuid
from contextlib import asynccontextmanager

import pytest
from httpx import ASGITransport, AsyncClient

from fdi.platform.config import get_settings

pytestmark = pytest.mark.integration


@pytest.fixture
def make_client(postgres_url, minio_endpoint, amqp_url, redis_url, monkeypatch, _alembic_applied):
    # `_alembic_applied` is this suite's migration gate; /extract/document
    # touches the document table, so the schema must exist.
    @asynccontextmanager
    async def _make(**overrides: str):
        env = {
            "FDI_DATABASE_URL": postgres_url,
            "FDI_BROKER_URL": amqp_url,
            "FDI_RESULT_BACKEND": redis_url,
            "FDI_S3_ENDPOINT": minio_endpoint,
            "FDI_S3_ACCESS_KEY": "minioadmin",
            "FDI_S3_SECRET_KEY": "minioadmin",
            # A per-test bucket, so one test's PUT cannot leak into another's
            # GET through the shared session-scoped MinIO container.
            "FDI_S3_BUCKET": f"schema-{uuid.uuid4().hex[:12]}",
        }
        env.update(overrides)
        for key, value in env.items():
            monkeypatch.setenv(key, value)
        get_settings.cache_clear()

        from fdi.api.app import create_app

        app = create_app()
        async with app.router.lifespan_context(app):
            # The bucket is NOT created by lifespan (only /ready's probe calls
            # ensure_bucket), and this fixture uses a fresh per-test bucket
            # name, so it must be made here or the first put_json 404s.
            await app.state.artifact_store.ensure_bucket()
            transport = ASGITransport(app=app, raise_app_exceptions=True)
            async with AsyncClient(transport=transport, base_url="http://test") as client:
                yield client

    return _make


async def test_get_schema_falls_back_to_the_on_disk_seed(make_client) -> None:
    async with make_client() as client:
        r = await client.get("/schema")
    assert r.status_code == 200
    body = r.json()
    assert body["source"] == "seed"
    assert body["version"] == "4.2.0"
    assert body["field_count"] == 402
    assert len(body["schema_document"]["modules"]) == 4


async def test_put_schema_persists_and_bumps_the_patch_version(make_client) -> None:
    async with make_client() as client:
        current = (await client.get("/schema")).json()
        doc = current["schema_document"]
        doc["modules"][0]["retrieval_units"][0]["locator_keywords"].append("edited marker")

        put = await client.put(
            "/schema", json={"base_version": current["version"], "schema_document": doc}
        )
        assert put.status_code == 200
        assert put.json()["version"] == "4.2.1"
        assert put.json()["source"] == "store"

        after = (await client.get("/schema")).json()

    assert after["source"] == "store"
    assert after["version"] == "4.2.1"
    assert "edited marker" in after["schema_document"]["modules"][0][
        "retrieval_units"
    ][0]["locator_keywords"]


async def test_put_schema_with_a_stale_base_version_is_409(make_client) -> None:
    async with make_client() as client:
        current = (await client.get("/schema")).json()
        r = await client.put(
            "/schema",
            json={"base_version": "1.0.0", "schema_document": current["schema_document"]},
        )
    assert r.status_code == 409
    assert "1.0.0" in r.json()["detail"]
    assert current["version"] in r.json()["detail"]


async def test_put_schema_rejects_an_invalid_document(make_client) -> None:
    async with make_client() as client:
        current = (await client.get("/schema")).json()
        broken = {"version": "3.1.0", "modules": [{"key": "m", "name": "M", "fields": []}]}
        r = await client.put(
            "/schema", json={"base_version": current["version"], "schema_document": broken}
        )
        # The rejected write must not have landed.
        after = (await client.get("/schema")).json()
    assert r.status_code == 422
    # RFC 9457 framing (fdi.api.errors): `detail` is a string and the
    # structured per-path list rides alongside as an `errors` extension
    # member -- the same shape the request-validation 422 already uses.
    assert r.json()["detail"] == "schema is invalid"
    assert any("retrieval_units" in e for e in r.json()["errors"])
    assert after["source"] == "seed"


async def test_post_schema_validate_reports_without_writing(make_client) -> None:
    async with make_client() as client:
        current = (await client.get("/schema")).json()
        ok = await client.post(
            "/schema/validate", json={"schema_document": current["schema_document"]}
        )
        bad = await client.post("/schema/validate", json={"schema_document": {"modules": []}})
        after = (await client.get("/schema")).json()

    assert ok.status_code == 200
    assert ok.json() == {
        "valid": True, "errors": [], "module_count": 4, "unit_count": 4, "field_count": 402,
    }
    assert bad.status_code == 200
    assert bad.json()["valid"] is False
    assert bad.json()["field_count"] == 0
    assert after["source"] == "seed"


async def test_extract_document_reads_the_stored_schema(make_client) -> None:
    """The whole point of making the schema writable: an edit takes effect on
    the next extraction with no redeploy. Proven by shrinking the schema to
    one module and checking /extract/document 404s on an unknown doc_key
    AFTER the schema resolves -- i.e. the stored copy was the one loaded."""
    async with make_client() as client:
        current = (await client.get("/schema")).json()
        doc = current["schema_document"]
        doc["modules"] = doc["modules"][:1]
        put = await client.put(
            "/schema", json={"base_version": current["version"], "schema_document": doc}
        )
        assert put.status_code == 200
        assert put.json()["field_count"] < current["field_count"]

        r = await client.post("/extract/document", json={"doc_key": "no-such-key"})
    assert r.status_code in (404, 503)
