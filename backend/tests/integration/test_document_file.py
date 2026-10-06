"""GET /documents/{id}/file -- the source PDF, proxied through the API.

Proxied, not presigned: SP-18 sec 2 decision 1 (the browser would need MinIO
CORS configured to fetch localhost:9000 directly). An embedded PDF viewer is
the case that decision was made for.
"""

import uuid
from contextlib import asynccontextmanager
from urllib.parse import quote

import pytest
from httpx import ASGITransport, AsyncClient

from fdi.platform.config import get_settings
from tests.integration.conftest import authenticate

pytestmark = pytest.mark.integration

PDF_BYTES = b"%PDF-1.7\n%\xe2\xe3\xcf\xd3\n1 0 obj\n<<>>\nendobj\ntrailer\n%%EOF\n"


@pytest.fixture
async def make_client(
    postgres_url, minio_endpoint, amqp_url, redis_url, monkeypatch,
    _alembic_applied, invitee):
    # `_alembic_applied` is this suite's migration gate (tests/integration/
    # conftest.py); without it the schema is empty and the very first
    # `POST /documents` fails on `relation "document" does not exist`.
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
            # `artifacts` is the default bucket the app opens (this fixture
            # never sets FDI_S3_BUCKET). Lifespan does not create it -- only
            # /ready's probe calls ensure_bucket -- so intake's put_bytes
            # would 404 without this.
            await app.state.artifact_store.ensure_bucket()
            transport = ASGITransport(app=app, raise_app_exceptions=True)
            async with AsyncClient(transport=transport, base_url="http://test") as client:
                await authenticate(client, invitee)
                yield client

    return _make


async def test_file_returns_the_uploaded_pdf_bytes_inline(make_client) -> None:
    async with make_client() as client:
        upload = await client.post(
            "/documents",
            files={"file": ("ACME — FY24.pdf", PDF_BYTES, "application/pdf")},
            data={"doc_key": f"acme-{uuid.uuid4().hex}"},
        )
        assert upload.status_code == 201
        doc_id = upload.json()["id"]

        r = await client.get(f"/documents/{doc_id}/file")

    assert r.status_code == 200
    assert r.content == PDF_BYTES
    assert r.headers["content-type"] == "application/pdf"
    assert r.headers["content-length"] == str(len(PDF_BYTES))
    disposition = r.headers["content-disposition"]
    assert disposition.startswith("inline;")
    # Non-ASCII survives via RFC 5987, and the plain filename= stays ASCII-safe.
    assert f"filename*=UTF-8''{quote('ACME — FY24.pdf')}" in disposition
    assert 'filename="ACME _ FY24.pdf"' in disposition


async def test_file_does_not_advertise_range_support(make_client) -> None:
    """Range support is deliberately deferred (ArtifactStore reads whole
    bodies). Emitting no Accept-Ranges is the correct signal -- advertising
    `bytes` while ignoring the header is what makes a viewer render garbage."""
    async with make_client() as client:
        upload = await client.post(
            "/documents",
            files={"file": ("a.pdf", PDF_BYTES, "application/pdf")},
            data={"doc_key": f"acme-{uuid.uuid4().hex}"},
        )
        doc_id = upload.json()["id"]
        r = await client.get(f"/documents/{doc_id}/file")
    assert "accept-ranges" not in r.headers


async def test_file_unknown_document_is_404(make_client) -> None:
    async with make_client() as client:
        r = await client.get(f"/documents/{uuid.uuid4()}/file")
    assert r.status_code == 404
    assert "not found" in r.json()["detail"]


async def test_file_document_without_a_checksum_is_404(make_client, migrated_engine) -> None:
    """An early-intake row (RegistryRepository.create_document) has no
    checksum, so there is no content-addressed blob to serve."""
    from fdi.platform.db.engine import make_session_factory
    from fdi.platform.db.repository import RegistryRepository

    doc_id = str(uuid.uuid4())
    session_factory = make_session_factory(migrated_engine)
    async with session_factory() as s:
        await RegistryRepository(s).create_document(doc_id)
        await s.commit()

    async with make_client() as client:
        r = await client.get(f"/documents/{doc_id}/file")
    assert r.status_code == 404
    assert "no source file" in r.json()["detail"]
