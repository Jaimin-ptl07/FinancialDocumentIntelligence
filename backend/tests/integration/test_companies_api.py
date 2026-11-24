"""`/api/v1/companies`: what an Invitee sees of the company registry.

Companies are Public filings (2026-10-07 decision): every Invitee sees every
Company and its Reports, so the tests read as two different Invitees. Data is
seeded through the real path -- intake, the stage chain to `issuer`, then
`CompanyRegistry.resolve_report` -- not by writing rows."""

import uuid

import pytest

from fdi.pipeline.companies import CompanyRegistry
from tests.integration.conftest import acme_report, fresh_cin, ingest

pytestmark = pytest.mark.integration


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
    (report,) = reports.json()["reports"]
    assert report.pop("link_reason").startswith("CIN printed beside the issuer's name")
    assert report == {
        "doc_id": filed_report["doc_id"],
        "doc_key": filed_report["doc_key"],
        "version": 1,
        "filename": f"{filed_report['doc_key']}.pdf",
        "fiscal_year": None,
        "fiscal_year_confidence": "none",
        "fiscal_year_reason": "no Fiscal year is printed in the Report",
        "fiscal_year_method": None,
        "period_start": None,
        "period_end": None,
        "report_type": "annual_report",
        "company_id": company_id,
        "link_status": "linked",
        "link_method": "cin",
        "candidates": None,
        "last_change": None,
        "bases_present": [],
        "processing_status": "processing",
    }


async def _index_done(session_factory, doc_id: str) -> None:
    """The Report finished its `index` stage: it is searchable, so ready to
    research. Recorded as the worker records it, without running Milvus."""
    from fdi.platform.db.repository import RegistryRepository

    run_id = str(uuid.uuid4())
    async with session_factory() as s:
        repo = RegistryRepository(s)
        await repo.start_run(run_id, doc_id, "index", 1, "in", "c1")
        await repo.finish_run(run_id, "out", f"{doc_id}/index/1/in.json")
        await s.commit()


async def test_a_companys_reports_come_newest_fiscal_year_first_with_what_each_covers(
    make_client, invitee, session_factory, store
) -> None:
    cin = fresh_cin()
    statements = {3: "Standalone Balance Sheet", 4: "Consolidated Balance Sheet"}
    # FY2025-26 uploaded first, FY2024-25 after it: upload order is not the order.
    newer = await ingest(
        session_factory, store, doc_key=f"acme-2025-26-{uuid.uuid4()}",
        lines=[*acme_report(cin), "for the year ended 31st March 2026", "Total assets"],
        headings=statements,
    )
    older = await ingest(
        session_factory, store, doc_key=f"acme-2024-25-{uuid.uuid4()}",
        lines=[*acme_report(cin), "for the year ended 31st March 2025"],
        headings={3: "Consolidated Balance Sheet"},
    )
    async with session_factory() as s:
        registry = CompanyRegistry(s, store)
        company_id = (await registry.resolve_report(newer)).company_id
        await registry.resolve_report(older)
        await s.commit()
    await _index_done(session_factory, newer)

    async with make_client(invitee) as client:
        r = await client.get(f"/api/v1/companies/{company_id}/reports")

    assert r.status_code == 200
    reports = r.json()["reports"]
    assert [rep["doc_id"] for rep in reports] == [newer, older]
    covered = [
        {k: rep[k] for k in (
            "fiscal_year", "period_start", "period_end", "report_type",
            "link_status", "link_method", "bases_present", "processing_status",
        )}
        for rep in reports
    ]
    assert covered == [
        {
            "fiscal_year": "FY2025-26", "period_start": "2025-04-01",
            "period_end": "2026-03-31", "report_type": "annual_report",
            "link_status": "linked", "link_method": "cin",
            "bases_present": ["standalone", "consolidated"], "processing_status": "ready",
        },
        {
            "fiscal_year": "FY2024-25", "period_start": "2024-04-01",
            "period_end": "2025-03-31", "report_type": "annual_report",
            "link_status": "linked", "link_method": "cin",
            "bases_present": ["consolidated"], "processing_status": "processing",
        },
    ]


async def test_a_fiscal_year_the_report_contradicts_is_left_open_with_the_reason(
    make_client, invitee, session_factory, store
) -> None:
    cin = fresh_cin()
    doc_id = await ingest(
        session_factory, store, doc_key=f"acme-{uuid.uuid4()}",
        lines=[
            "Annual Report 2024-25",
            *acme_report(cin),
            "for the year ended 31st March 2026",
        ],
    )
    async with session_factory() as s:
        company_id = (await CompanyRegistry(s, store).resolve_report(doc_id)).company_id
        await s.commit()

    async with make_client(invitee) as client:
        r = await client.get(f"/api/v1/companies/{company_id}/reports")

    (report,) = r.json()["reports"]
    assert report["fiscal_year"] is None
    assert report["fiscal_year_confidence"] == "low"
    assert "FY2024-25" in report["fiscal_year_reason"]
    assert "FY2025-26" in report["fiscal_year_reason"]


async def test_an_unknown_company_is_404(make_client, invitee) -> None:
    unknown = str(uuid.uuid4())
    async with make_client(invitee) as client:
        assert (await client.get(f"/api/v1/companies/{unknown}")).status_code == 404
        assert (await client.get(f"/api/v1/companies/{unknown}/reports")).status_code == 404


async def test_companies_need_a_login(make_client) -> None:
    async with make_client(None) as client:
        assert (await client.get("/api/v1/companies")).status_code == 401
