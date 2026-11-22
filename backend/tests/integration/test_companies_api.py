"""`/api/v1/companies`: what an Invitee sees of the company registry.

Companies are Public filings (2026-10-07 decision): every Invitee sees every
Company and its Reports, so the tests read as two different Invitees. Data is
seeded through the real path -- intake, the stage chain to `issuer`, then
`CompanyRegistry.resolve_report` -- not by writing rows."""

import uuid
from contextlib import asynccontextmanager

import pytest
from httpx import ASGITransport, AsyncClient

from fdi.pipeline.companies import CompanyRegistry
from fdi.platform.config import get_settings
from fdi.platform.db.repository import UserRepository
from fdi.platform.security import hash_password
from tests.conftest import TEST_SIGNING_SECRET
from tests.integration.conftest import (
    INVITEE_PASSWORD,
    acme_report,
    authenticate,
    fresh_cin,
    ingest,
)

pytestmark = pytest.mark.integration


@pytest.fixture
async def make_client(postgres_url, minio_endpoint, amqp_url, redis_url, monkeypatch):
    @asynccontextmanager
    async def _make(creds: tuple[str, str] | None):
        for key, value in {
            "FDI_DATABASE_URL": postgres_url,
            "FDI_BROKER_URL": amqp_url,
            "FDI_RESULT_BACKEND": redis_url,
            "FDI_S3_ENDPOINT": minio_endpoint,
            "FDI_S3_ACCESS_KEY": "minioadmin",
            "FDI_S3_SECRET_KEY": "minioadmin",
            "FDI_S3_BUCKET": "test-artifacts",
            "FDI_AUTH_SECRET": TEST_SIGNING_SECRET,
        }.items():
            monkeypatch.setenv(key, value)
        get_settings.cache_clear()

        from fdi.api.app import create_app

        app = create_app()
        async with app.router.lifespan_context(app):
            transport = ASGITransport(app=app)
            async with AsyncClient(transport=transport, base_url="http://test") as client:
                if creds is not None:
                    await authenticate(client, creds)
                yield client

    return _make


@pytest.fixture
async def another_invitee(session_factory) -> tuple[str, str]:
    email = f"invitee-{uuid.uuid4().hex}@example.test"
    async with session_factory() as s:
        await UserRepository(s).create(email=email, password_hash=hash_password(INVITEE_PASSWORD))
        await s.commit()
    return email, INVITEE_PASSWORD


@pytest.fixture
async def filed_report(session_factory, store) -> dict[str, str]:
    cin = fresh_cin()
    doc_key = f"acme-{uuid.uuid4()}"
    doc_id = await ingest(session_factory, store, doc_key=doc_key, lines=acme_report(cin))
    async with session_factory() as s:
        link = await CompanyRegistry(s, store).resolve_report(doc_id)
        await s.commit()
    assert link.company_id is not None
    return {"cin": cin, "doc_key": doc_key, "doc_id": doc_id, "company_id": link.company_id}


async def test_every_invitee_sees_every_company(
    make_client, invitee, another_invitee, filed_report
) -> None:
    for creds in (invitee, another_invitee):
        async with make_client(creds) as client:
            r = await client.get("/api/v1/companies")
        assert r.status_code == 200
        assert {
            "id": filed_report["company_id"],
            "cin": filed_report["cin"],
            "name": "Acme Widgets Limited",
        } in r.json()["companies"]


async def test_a_company_shows_its_registered_name_cin_and_reports(
    make_client, invitee, filed_report
) -> None:
    company_id = filed_report["company_id"]
    async with make_client(invitee) as client:
        company = await client.get(f"/api/v1/companies/{company_id}")
        reports = await client.get(f"/api/v1/companies/{company_id}/reports")

    assert company.status_code == 200
    assert company.json() == {
        "id": company_id, "cin": filed_report["cin"], "name": "Acme Widgets Limited",
    }
    assert reports.status_code == 200
    assert reports.json()["reports"] == [{
        "doc_id": filed_report["doc_id"],
        "doc_key": filed_report["doc_key"],
        "version": 1,
        "filename": f"{filed_report['doc_key']}.pdf",
    }]


async def test_an_unknown_company_is_404(make_client, invitee) -> None:
    unknown = str(uuid.uuid4())
    async with make_client(invitee) as client:
        assert (await client.get(f"/api/v1/companies/{unknown}")).status_code == 404
        assert (await client.get(f"/api/v1/companies/{unknown}/reports")).status_code == 404


async def test_companies_need_a_login(make_client) -> None:
    async with make_client(None) as client:
        assert (await client.get("/api/v1/companies")).status_code == 401
