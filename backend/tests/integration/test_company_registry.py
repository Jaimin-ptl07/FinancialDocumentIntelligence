"""The company registry links a Report to its Company by the issuer's CIN.

Each Report goes through the real stage chain (fake OCR -> ... -> issuer)
against Postgres + MinIO, then `CompanyRegistry.resolve_report` -- the one
path the worker, the backfill and the API all use. Assertions are on what
the registry answers (which Company, which Reports), never on its tables."""

import uuid

import pytest

from fdi.pipeline.companies import CompanyRegistry
from fdi.pipeline.intake import intake_document
from tests.integration.conftest import acme_report, fresh_cin, ingest

pytestmark = pytest.mark.integration

async def resolve(session_factory, store, doc_id: str):
    async with session_factory() as s:
        outcome = await CompanyRegistry(s, store).resolve_report(doc_id)
        await s.commit()
    return outcome


async def test_new_cin_creates_the_company_and_files_the_report_under_it(
    session_factory, store
) -> None:
    cin = fresh_cin()
    doc_key = f"acme-{uuid.uuid4()}"
    doc_id = await ingest(session_factory, store, doc_key=doc_key, lines=acme_report(cin))

    outcome = await resolve(session_factory, store, doc_id)

    assert outcome.cin == cin
    assert outcome.created_company
    async with session_factory() as s:
        registry = CompanyRegistry(s, store)
        company = await registry.get_company(outcome.company_id)
        reports = await registry.company_reports(outcome.company_id)
    assert company is not None
    assert (company.cin, company.name) == (cin, "Acme Widgets Limited")
    assert [r.doc_key for r in reports] == [doc_key]


async def test_a_second_report_with_a_known_cin_joins_the_same_company(
    session_factory, store
) -> None:
    cin = fresh_cin()
    first = await ingest(
        session_factory, store, doc_key=f"acme-a-{uuid.uuid4()}", lines=acme_report(cin)
    )
    second = await ingest(
        session_factory, store, doc_key=f"acme-b-{uuid.uuid4()}", lines=acme_report(cin)
    )

    a = await resolve(session_factory, store, first)
    b = await resolve(session_factory, store, second)

    assert a.company_id == b.company_id
    assert (a.created_company, b.created_company) == (True, False)
    async with session_factory() as s:
        reports = await CompanyRegistry(s, store).company_reports(a.company_id)
        companies = await CompanyRegistry(s, store).list_companies()
    assert len(reports) == 2
    assert [c.cin for c in companies].count(cin) == 1


async def test_resolving_twice_changes_nothing(session_factory, store) -> None:
    doc_id = await ingest(
        session_factory, store, doc_key=f"acme-{uuid.uuid4()}", lines=acme_report(fresh_cin())
    )

    a = await resolve(session_factory, store, doc_id)
    b = await resolve(session_factory, store, doc_id)

    assert a.company_id == b.company_id
    async with session_factory() as s:
        assert len(await CompanyRegistry(s, store).company_reports(a.company_id)) == 1


async def test_a_report_with_no_cin_is_left_unlinked_awaiting_an_invitee_with_the_reason(
    session_factory, store
) -> None:
    doc_id = await ingest(
        session_factory, store, doc_key=f"beta-{uuid.uuid4()}",
        lines=["Beta Power Limited had a record year."],
    )

    outcome = await resolve(session_factory, store, doc_id)

    assert outcome.company_id is None
    assert "no CIN" in outcome.reason
    # It names its issuer without a CIN: an Invitee confirms it (S2.3).
    assert outcome.link_status == "awaiting_confirmation"


async def test_cins_with_no_clear_issuer_await_confirmation(session_factory, store) -> None:
    doc_id = await ingest(
        session_factory, store, doc_key=f"gamma-{uuid.uuid4()}",
        lines=[
            "Reference numbers U12345MH2001PTC111111 and U54321MH2002PTC222222 apply.",
            "for the year ended 31st March 2026",
        ],
    )

    outcome = await resolve(session_factory, store, doc_id)

    assert outcome.company_id is None
    assert outcome.link_status == "awaiting_confirmation"
    assert outcome.fiscal_year == "FY2025-26"


async def test_a_linked_report_carries_its_fiscal_year(session_factory, store) -> None:
    doc_id = await ingest(
        session_factory, store, doc_key=f"acme-{uuid.uuid4()}",
        lines=[*acme_report(fresh_cin()), "for the period ended on 31st March, 2025"],
    )

    outcome = await resolve(session_factory, store, doc_id)

    assert (outcome.link_status, outcome.fiscal_year) == ("linked", "FY2024-25")
    async with session_factory() as s:
        (report,) = await CompanyRegistry(s, store).company_reports(outcome.company_id)
    assert (report.fiscal_year, str(report.period_start), str(report.period_end)) == (
        "FY2024-25", "2024-04-01", "2025-03-31",
    )
    assert (report.link_status, report.link_method) == ("linked", "cin")


async def test_a_report_never_run_through_issuer_is_left_unlinked(
    session_factory, store
) -> None:
    doc = await intake_document(
        pdf=f"%PDF-1.7\n%fake-pdf-body-{uuid.uuid4()}\n".encode(),
        filename="x.pdf", doc_key=f"x-{uuid.uuid4()}", content_type="application/pdf",
        session_factory=session_factory, store=store,
    )

    outcome = await resolve(session_factory, store, doc.id)

    assert outcome.company_id is None
    assert "issuer" in outcome.reason
