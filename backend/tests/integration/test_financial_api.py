"""`/api/v1/companies/{id}/financial-*` and `/api/v1/analytics/*` (S5): what an
Invitee gets over HTTP. Exact decimals travel as strings."""

import pytest
from minio import Minio
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from fdi.pipeline.financial_facts import FinancialFactService
from fdi.platform.db.engine import make_session_factory
from fdi.platform.storage.artifact_store import ArtifactStore
from tests.integration.financial_fixtures import SCHEMA, Seeded, acme_specs, seed_company

pytestmark = pytest.mark.integration


@pytest.fixture
def session_factory(migrated_engine: AsyncEngine) -> async_sessionmaker[AsyncSession]:
    return make_session_factory(migrated_engine)


@pytest.fixture
async def store(minio_client: Minio) -> ArtifactStore:
    s = ArtifactStore(minio_client, bucket="test-artifacts")
    await s.ensure_bucket()
    return s


async def _acme(session_factory, store, name: str = "ACME") -> Seeded:  # type: ignore[no-untyped-def]
    async with session_factory() as s:
        await FinancialFactService(session=s, store=store).seed_concepts(SCHEMA)
        await s.commit()
        seeded = await seed_company(s, store, acme_specs(), name=name)
    for doc_id in seeded.docs.values():
        async with session_factory() as s:
            await FinancialFactService(session=s, store=store, formulas_schema=SCHEMA).project(
                doc_id)
    return seeded


@pytest.fixture
async def acme(session_factory, store) -> Seeded:
    return await _acme(session_factory, store)


async def test_financial_facts_with_observations_and_gaps(make_client, invitee, acme) -> None:
    async with make_client(invitee) as client:
        r = await client.get(
            f"/api/v1/companies/{acme.company_id}/financial-facts",
            params=[("concept", "RFOGRS"), ("concept", "PBTR"), ("fiscal_year", "FY2024-25"),
                    ("fiscal_year", "FY2025-26"), ("basis", "consolidated")],
        )
    assert r.status_code == 200, r.text
    body = r.json()
    facts = {(f["concept"], f["fiscal_year"]): f for f in body["facts"]}
    restated = facts[("RFOGRS", "FY2024-25")]
    assert (restated["status"], restated["value"], restated["restated"]) == (
        "restated", "255000", True)
    assert restated["restatement"]["original"]["value"] == "255324"
    assert restated["restatement"]["original"]["printed"] == "2,55,324"
    assert restated["unit"] == {"label": "INR crore", "currency": "INR", "scale": "10000000",
                                "printed": "₹ crore"}
    assert restated["normalised"] == "2550000000000"
    assert len(restated["observations"]) == 2
    assert all(e.endswith(":fact:consolidated:RFOGRS") for e in restated["evidence_ids"])
    current = facts[("RFOGRS", "FY2025-26")]
    assert current["checks"][0]["rule_id"] == "check:RFOGRS"
    assert current["selected"]["line_item_evidence_id"].endswith(
        ":li:st:consolidated:profit_and_loss:r0")
    assert {(g["concept"], g["fiscal_year"], g["reason"]) for g in body["gaps"]} == {
        # FY2024-25 is also the FY2025-26 Report's comparative column, and that
        # Report's PBT was asked for and never answered: missed, not not_found.
        ("PBTR", "FY2025-26", "missed"), ("PBTR", "FY2024-25", "missed")}


async def test_one_fact_and_a_missing_one(make_client, invitee, acme) -> None:
    base = f"/api/v1/companies/{acme.company_id}/financial-facts"
    async with make_client(invitee) as client:
        ok = await client.get(f"{base}/PATR/FY2025-26")
        missing = await client.get(f"{base}/PBTR/FY2025-26")
        no_company = await client.get(
            "/api/v1/companies/00000000-0000-0000-0000-000000000000/financial-facts/PATR/FY2025-26")
        bad_year = await client.get(f"{base}/PATR/2025")
    assert ok.status_code == 200 and ok.json()["printed"] == "49,454"
    assert missing.status_code == 404 and missing.json()["gap"] == "missed"
    assert missing.json()["detail"] == "no consolidated PBTR for FY2025-26: missed"
    assert no_company.status_code == 404
    assert bad_year.status_code == 422


async def test_financial_history_is_a_multi_year_table(make_client, invitee, acme) -> None:
    async with make_client(invitee) as client:
        r = await client.get(
            f"/api/v1/companies/{acme.company_id}/financial-history",
            params=[("concept", "PATR"), ("concept", "TOTCAR")],
        )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["fiscal_years"] == ["FY2023-24", "FY2024-25", "FY2025-26"]
    patr = next(row for row in body["rows"] if row["concept"] == "PATR")
    assert [c["fact"]["value"] if c["fact"] else c["gap"] for c in patr["cells"]] == [
        "46099", "48797", "49454"]
    totcar = next(row for row in body["rows"] if row["concept"] == "TOTCAR")
    assert totcar["cells"][0]["gap"] == "not_found"


async def test_the_metric_catalogue_and_calculate(make_client, invitee, acme) -> None:
    async with make_client(invitee) as client:
        catalogue = await client.get("/api/v1/analytics/metrics")
        calc = await client.post("/api/v1/analytics/calculate", json={
            "metric": "current_ratio", "company_id": acme.company_id,
            "fiscal_years": ["FY2025-26", "FY2023-24"]})
        unknown = await client.post("/api/v1/analytics/calculate", json={
            "metric": "vibes", "company_id": acme.company_id, "fiscal_years": ["FY2025-26"]})
        extra = await client.post("/api/v1/analytics/calculate", json={
            "metric": "current_ratio", "company_id": acme.company_id,
            "fiscal_years": ["FY2025-26"], "sql": "drop table"})
    assert catalogue.status_code == 200
    assert "operating_margin" in {m["name"] for m in catalogue.json()}
    assert calc.status_code == 200, calc.text
    [ok, refused] = calc.json()["results"]
    assert (ok["status"], ok["value"], ok["unit"], ok["formula"]) == (
        "ok", "2.2278", "ratio", "TOTCAR / TOTCLR")
    assert [i["concept"] for i in ok["inputs"]] == ["TOTCAR", "TOTCLR"]
    assert ok["inputs"][0]["evidence_ids"]
    assert (refused["status"], refused["value"]) == ("not_computable", None)
    assert refused["reasons"] == ["missing_input:current_assets:TOTCAR FY2023-24",
                                  "missing_input:current_liabilities:TOTCLR FY2023-24"]
    assert unknown.status_code == 422 and extra.status_code == 422


async def test_compare_periods_and_companies(
    make_client, invitee, acme, session_factory, store,
) -> None:
    peer = await _acme(session_factory, store, name="PEER")
    async with make_client(invitee) as client:
        periods = await client.post("/api/v1/analytics/compare-periods", json={
            "metric": "pat_margin", "company_id": acme.company_id,
            "fiscal_years": ["FY2025-26", "FY2024-25"]})
        companies = await client.post("/api/v1/analytics/compare-companies", json={
            "metric": "pat_margin", "company_ids": [acme.company_id, peer.company_id],
            "fiscal_year": "FY2025-26"})
        one = await client.post("/api/v1/analytics/compare-companies", json={
            "metric": "pat_margin", "company_ids": [acme.company_id],
            "fiscal_year": "FY2025-26"})
    assert periods.status_code == 200, periods.text
    p = periods.json()
    assert [r["fiscal_year"] for r in p["results"]] == ["FY2024-25", "FY2025-26"]
    assert p["results"][1]["value"] == "18.5206"                  # 49454 / 267021
    assert p["comparisons"][0]["comparable"] is False             # FY2024-25 revenue restated
    c = companies.json()
    assert [r["value"] for r in c["results"]] == ["18.5206", "18.5206"]
    assert c["comparisons"] == [{"comparable": True, "reasons": [], "warnings": []}]
    assert one.status_code == 422
