"""`/api/v1/workspaces`: an Invitee's personal working sets of Companies.

A Workspace is a focus list, never an access boundary (#16): it is visible
only to the Invitee who owns it, and it grants or hides nothing -- every
Invitee still sees every Company and its Reports. Someone else's Workspace is
the same 404 as no Workspace, so its existence is not revealed."""

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


async def _file_report(session_factory, store) -> dict[str, str]:
    cin = fresh_cin()
    doc_key = f"acme-{uuid.uuid4()}"
    doc_id = await ingest(session_factory, store, doc_key=doc_key, lines=acme_report(cin))
    async with session_factory() as s:
        link = await CompanyRegistry(s, store).resolve_report(doc_id)
        await s.commit()
    assert link.company_id is not None
    return {"cin": cin, "doc_key": doc_key, "doc_id": doc_id, "company_id": link.company_id}


@pytest.fixture
async def filed_report(session_factory, store) -> dict[str, str]:
    return await _file_report(session_factory, store)


async def test_an_invitee_creates_a_workspace_and_finds_it_in_their_list(
    make_client, invitee
) -> None:
    async with make_client(invitee) as client:
        created = await client.post("/api/v1/workspaces", json={"name": "Auto majors"})
        listed = await client.get("/api/v1/workspaces")
        got = await client.get(f"/api/v1/workspaces/{created.json()['id']}")

    assert created.status_code == 201
    workspace = created.json()
    assert workspace["name"] == "Auto majors"
    assert workspace["companies"] == []
    assert listed.status_code == 200
    assert listed.json()["workspaces"] == [{"id": workspace["id"], "name": "Auto majors"}]
    assert got.status_code == 200
    assert got.json() == workspace


async def test_an_invitee_adds_and_removes_companies(
    make_client, invitee, session_factory, store
) -> None:
    acme = await _file_report(session_factory, store)
    other = await _file_report(session_factory, store)
    async with make_client(invitee) as client:
        ws = (await client.post("/api/v1/workspaces", json={"name": "Watchlist"})).json()
        base = f"/api/v1/workspaces/{ws['id']}/companies"
        added = await client.put(f"{base}/{acme['company_id']}")
        again = await client.put(f"{base}/{acme['company_id']}")
        await client.put(f"{base}/{other['company_id']}")
        with_both = (await client.get(f"/api/v1/workspaces/{ws['id']}")).json()
        removed = await client.delete(f"{base}/{acme['company_id']}")
        after = (await client.get(f"/api/v1/workspaces/{ws['id']}")).json()

    assert added.status_code == 200
    assert again.status_code == 200
    assert {c["id"] for c in with_both["companies"]} == {
        acme["company_id"], other["company_id"],
    }
    assert {"id": acme["company_id"], "cin": acme["cin"], "name": "Acme Widgets Limited"} in (
        with_both["companies"]
    )
    assert removed.status_code == 200
    assert [c["id"] for c in after["companies"]] == [other["company_id"]]
    assert removed.json() == after


async def test_adding_an_unknown_company_is_404(make_client, invitee) -> None:
    async with make_client(invitee) as client:
        ws = (await client.post("/api/v1/workspaces", json={"name": "Watchlist"})).json()
        r = await client.put(f"/api/v1/workspaces/{ws['id']}/companies/{uuid.uuid4()}")
        after = (await client.get(f"/api/v1/workspaces/{ws['id']}")).json()
    assert r.status_code == 404
    assert after["companies"] == []


async def test_another_invitee_cannot_list_read_or_modify_a_workspace(
    make_client, invitee, another_invitee, filed_report
) -> None:
    company_id = filed_report["company_id"]
    async with make_client(invitee) as owner:
        ws = (await owner.post("/api/v1/workspaces", json={"name": "Mine"})).json()
        await owner.put(f"/api/v1/workspaces/{ws['id']}/companies/{company_id}")
    path = f"/api/v1/workspaces/{ws['id']}"

    async with make_client(another_invitee) as stranger:
        listed = await stranger.get("/api/v1/workspaces")
        attempts = {
            "get": await stranger.get(path),
            "rename": await stranger.patch(path, json={"name": "Theirs"}),
            "add": await stranger.put(f"{path}/companies/{company_id}"),
            "remove": await stranger.delete(f"{path}/companies/{company_id}"),
            "delete": await stranger.delete(path),
        }
        missing = await stranger.get(f"/api/v1/workspaces/{uuid.uuid4()}")

    assert listed.status_code == 200
    assert ws["id"] not in {w["id"] for w in listed.json()["workspaces"]}
    # Indistinguishable from a Workspace that does not exist: 404, same body
    # shape, never 403.
    for verb, r in attempts.items():
        assert r.status_code == 404, verb
        assert r.json().keys() == missing.json().keys(), verb
    async with make_client(invitee) as owner:
        after = (await owner.get(path)).json()
    assert after["name"] == "Mine"
    assert [c["id"] for c in after["companies"]] == [company_id]


async def test_an_invitee_without_workspaces_still_sees_every_company_and_report(
    make_client, invitee, another_invitee, filed_report, session_factory, store
) -> None:
    company_id = filed_report["company_id"]
    elsewhere = await _file_report(session_factory, store)
    # The owner's only Workspace holds some other Company, leaving this one out.
    async with make_client(invitee) as owner:
        ws = (await owner.post("/api/v1/workspaces", json={"name": "Elsewhere"})).json()
        added = await owner.put(
            f"/api/v1/workspaces/{ws['id']}/companies/{elsewhere['company_id']}"
        )
        assert [c["id"] for c in added.json()["companies"]] == [elsewhere["company_id"]]
        excluded_view = await _company_reads(owner, company_id)
    # A second Invitee with no Workspaces at all.
    async with make_client(another_invitee) as bare:
        assert (await bare.get("/api/v1/workspaces")).json()["workspaces"] == []
        bare_view = await _company_reads(bare, company_id)

    for view in (excluded_view, bare_view):
        assert filed_report["company_id"] in view["listed"]
        assert view["company"] == 200
        assert view["report_ids"] == [filed_report["doc_id"]]


async def _company_reads(client: AsyncClient, company_id: str) -> dict[str, object]:
    listed = (await client.get("/api/v1/companies")).json()["companies"]
    company = await client.get(f"/api/v1/companies/{company_id}")
    reports = (await client.get(f"/api/v1/companies/{company_id}/reports")).json()
    return {
        "listed": {c["id"] for c in listed},
        "company": company.status_code,
        "report_ids": [r["doc_id"] for r in reports["reports"]],
    }


async def test_workspaces_need_a_login(make_client) -> None:
    async with make_client(None) as client:
        assert (await client.get("/api/v1/workspaces")).status_code == 401
        assert (
            await client.post("/api/v1/workspaces", json={"name": "x"})
        ).status_code == 401


async def test_an_invitee_renames_and_deletes_a_workspace(make_client, invitee) -> None:
    async with make_client(invitee) as client:
        ws = (await client.post("/api/v1/workspaces", json={"name": "Draft"})).json()
        renamed = await client.patch(f"/api/v1/workspaces/{ws['id']}", json={"name": "Final"})
        deleted = await client.delete(f"/api/v1/workspaces/{ws['id']}")
        gone = await client.get(f"/api/v1/workspaces/{ws['id']}")
        listed = (await client.get("/api/v1/workspaces")).json()

    assert renamed.status_code == 200
    assert renamed.json()["name"] == "Final"
    assert deleted.status_code == 204
    assert gone.status_code == 404
    assert ws["id"] not in {w["id"] for w in listed["workspaces"]}
