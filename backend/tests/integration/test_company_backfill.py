"""The backfill files already-ingested Reports by the same path as new ones:
the issuer stage, then `CompanyRegistry.resolve_report`.

The three corpus Reports are rebuilt from passages cut from their real text,
run through the stage chain up to provenance (as they were ingested before
the issuer stage existed), then backfilled. REMC's was uploaded under the
wrong issuer's key, `rites-2024-25`; the backfill must still file it under
REMC Limited."""

import uuid

import pytest

from fdi.pipeline.companies import CompanyRegistry, backfill_companies
from tests.integration.conftest import acme_report, fresh_cin, ingest

pytestmark = pytest.mark.integration

TCS_CIN = "L22210MH1995PLC084781"
RIL_CIN = "L17110MH1973PLC019786"
REMC_CIN = "U93000DL2013GOI256661"

TCS = [
    "Tata Consultancy Services Limited delivered growth.",
    "We have examined the relevant registers, records, forms, returns and disclosures "
    "received from the Directors of Tata Consultancy Services Limited having CIN "
    f"{TCS_CIN} and having registered office at 9th Floor, Nirmal Building, Mumbai",
    "Consolidated Balance Sheet as at March 31, 2026, for the year ended March 31, 2026",
]
RIL = [
    "Reliance Industries Limited had a record year. Reliance Industries Limited grew.",
    "I have examined the relevant registers, records, forms, returns and disclosures "
    f"received from the Directors of Reliance Industries Limited having CIN {RIL_CIN} "
    "and registered office at 3rd Floor, Maker Chambers IV, Nariman Point, Mumbai",
    "To, The Members, Reliance Retail Ventures Limited CIN: U51909MH2006PLC166166 "
    "4th Floor, Court House, Lokmanya Tilak Marg, Mumbai",
    "To, The Members, Reliance Retail Ventures Limited CIN: U51909MH2006PLC166166 "
    "4th Floor, Court House, Lokmanya Tilak Marg, Mumbai",
    "Year ended 31st March, 2026 Year ended 31st March, 2025",
    "Year ended 31st March, 2026 Year ended 31st March, 2025",
]
# RIL holds both statement sets; the others' fixtures hold one or none.
RIL_STATEMENTS = {
    5: "Standalone Statement of Profit and Loss for the year ended 31st March, 2026",
    6: "Consolidated Statement of Profit and Loss for the year ended 31st March, 2026",
}
REMC = [
    "REMC Limited (A Joint Venture of Indian Railways and RITES Limited)",
    "We have examined the compliance of conditions of Corporate Governance by REMC "
    f"LIMITED (CIN: {REMC_CIN}) for the period ended on 31st March, 2025",
]


async def test_backfill_files_every_corpus_report_under_its_company(
    session_factory, store
) -> None:
    suffix = uuid.uuid4().hex[:8]
    docs = {}
    for doc_key, lines, headings in (
        (f"tcs-2025-26-{suffix}", TCS, None),
        (f"ril-2025-26-{suffix}", RIL, RIL_STATEMENTS),
        (f"rites-2024-25-{suffix}", REMC, None),
    ):
        # Ingested before the issuer stage existed: the chain stops at basis.
        docs[doc_key] = await ingest(
            session_factory, store, doc_key=doc_key, lines=lines, through="basis",
            headings=headings,
        )

    links = await backfill_companies(session_factory=session_factory, store=store)

    filed = {link.doc_id: link for link in links}
    async with session_factory() as s:
        registry = CompanyRegistry(s, store)
        by_doc = {}
        covers = {}
        for doc_key, doc_id in docs.items():
            company = await registry.get_company(filed[doc_id].company_id)
            assert company is not None, f"{doc_key} was not filed: {filed[doc_id].reason}"
            short = doc_key.rsplit("-", 1)[0]
            by_doc[short] = (company.cin, company.name)
            (report,) = [
                r for r in await registry.company_reports(company.id) if r.doc_id == doc_id
            ]
            covers[short] = (report.fiscal_year, report.bases_present)
        remc_id = filed[docs[f"rites-2024-25-{suffix}"]].company_id
        remc_reports = await registry.company_reports(remc_id)

    assert by_doc == {
        "tcs-2025-26": (TCS_CIN, "Tata Consultancy Services Limited"),
        "ril-2025-26": (RIL_CIN, "Reliance Industries Limited"),
        "rites-2024-25": (REMC_CIN, "REMC Limited"),
    }
    assert covers == {
        "tcs-2025-26": ("FY2025-26", ()),
        "ril-2025-26": ("FY2025-26", ("standalone", "consolidated")),
        "rites-2024-25": ("FY2024-25", ()),
    }
    assert f"rites-2024-25-{suffix}" in [r.doc_key for r in remc_reports]


async def test_backfill_skips_a_report_with_no_text_yet(session_factory, store) -> None:
    doc_id = await ingest(
        session_factory, store, doc_key=f"early-{uuid.uuid4()}", lines=["x"], through="ocr"
    )

    links = await backfill_companies(session_factory=session_factory, store=store)

    assert doc_id not in {link.doc_id for link in links}


async def test_one_report_failing_does_not_stop_the_others(session_factory, store) -> None:
    from fdi.platform.db.repository import RegistryRepository

    broken = await ingest(
        session_factory, store, doc_key=f"broken-{uuid.uuid4()}",
        lines=acme_report(fresh_cin()), through="provenance",
    )
    cin = fresh_cin()
    healthy = await ingest(
        session_factory, store, doc_key=f"healthy-{uuid.uuid4()}",
        lines=acme_report(cin), through="provenance",
    )
    async with session_factory() as s:
        ast_run = await RegistryRepository(s).latest_completed_run(broken, "ast")
    assert ast_run is not None and ast_run.artifact_key is not None
    await store.delete(ast_run.artifact_key)  # the issuer stage cannot read its input

    links = {link.doc_id: link for link in await backfill_companies(
        session_factory=session_factory, store=store
    )}

    assert links[healthy].cin == cin
    assert links[broken].company_id is None
    assert "failed" in links[broken].reason
