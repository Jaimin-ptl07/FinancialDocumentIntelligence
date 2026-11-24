"""A corrected re-upload is a new version of the same Report (S2.6, #53).

Each upload goes through intake and the real stage chain (fake OCR -> ... ->
issuer), then `CompanyRegistry.resolve_report`. Assertions are on what an
Invitee sees: the Company's history (`company_reports`), one entry per
Report, newest Fiscal year first."""

import uuid

import pytest

from fdi.pipeline.companies import CompanyRegistry
from fdi.platform.db.repository import DocumentRepository
from tests.integration.conftest import acme_report, fresh_cin, ingest

pytestmark = pytest.mark.integration


async def upload_and_file(session_factory, store, *, doc_key: str, lines: list[str]):
    doc_id = await ingest(session_factory, store, doc_key=doc_key, lines=lines)
    async with session_factory() as s:
        link = await CompanyRegistry(s, store).resolve_report(doc_id)
        await s.commit()
    assert link.company_id is not None
    return doc_id, link.company_id


async def history(session_factory, store, company_id: str):
    async with session_factory() as s:
        return await CompanyRegistry(s, store).company_reports(company_id)


def fy2025(cin: str) -> list[str]:
    return [*acme_report(cin), "for the year ended 31st March 2025"]


async def test_a_corrected_copy_under_the_same_doc_key_is_a_second_version_of_one_report(
    session_factory, store
) -> None:
    cin, doc_key = fresh_cin(), f"acme-{uuid.uuid4()}"
    first, company_id = await upload_and_file(
        session_factory, store, doc_key=doc_key, lines=fy2025(cin)
    )
    corrected, _ = await upload_and_file(
        session_factory, store, doc_key=doc_key, lines=fy2025(cin)
    )

    (report,) = await history(session_factory, store, company_id)

    assert first != corrected
    assert (report.doc_id, report.doc_key, report.version) == (corrected, doc_key, 2)
    assert report.fiscal_year == "FY2024-25"


async def test_the_same_issuer_and_fiscal_year_under_another_doc_key_shows_that_year_once(
    session_factory, store
) -> None:
    cin = fresh_cin()
    old_key, new_key = f"acme-fy25-{uuid.uuid4()}", f"acme-2024-25-{uuid.uuid4()}"
    older, company_id = await upload_and_file(
        session_factory, store, doc_key=old_key, lines=fy2025(cin)
    )
    newer, _ = await upload_and_file(session_factory, store, doc_key=new_key, lines=fy2025(cin))

    reports = await history(session_factory, store, company_id)

    assert [(r.doc_id, r.doc_key, r.fiscal_year) for r in reports] == [
        (newer, new_key, "FY2024-25")
    ]
    assert older != newer

    # The older upload is not re-keyed: its doc_key still names its own Report.
    async with session_factory() as s:
        assert [d.id for d in await DocumentRepository(s).list_by_doc_key(old_key)] == [older]


async def test_reports_with_no_fiscal_year_are_never_merged(session_factory, store) -> None:
    cin = fresh_cin()
    a, company_id = await upload_and_file(
        session_factory, store, doc_key=f"acme-a-{uuid.uuid4()}", lines=acme_report(cin)
    )
    b, _ = await upload_and_file(
        session_factory, store, doc_key=f"acme-b-{uuid.uuid4()}", lines=acme_report(cin)
    )

    reports = await history(session_factory, store, company_id)

    assert {r.doc_id for r in reports} == {a, b}
    assert all(r.fiscal_year is None for r in reports)


async def test_different_fiscal_years_under_other_doc_keys_are_each_their_own_report(
    session_factory, store
) -> None:
    cin = fresh_cin()
    fy25, company_id = await upload_and_file(
        session_factory, store, doc_key=f"acme-{uuid.uuid4()}", lines=fy2025(cin)
    )
    fy26, _ = await upload_and_file(
        session_factory, store, doc_key=f"acme-{uuid.uuid4()}",
        lines=[*acme_report(cin), "for the year ended 31st March 2026"],
    )

    reports = await history(session_factory, store, company_id)

    assert [(r.doc_id, r.fiscal_year) for r in reports] == [
        (fy26, "FY2025-26"), (fy25, "FY2024-25"),
    ]


async def test_a_corrected_version_that_changes_the_fiscal_year_still_shows_once(
    session_factory, store
) -> None:
    cin, doc_key = fresh_cin(), f"acme-{uuid.uuid4()}"
    await upload_and_file(session_factory, store, doc_key=doc_key, lines=fy2025(cin))
    corrected, company_id = await upload_and_file(
        session_factory, store, doc_key=doc_key,
        lines=[*acme_report(cin), "for the year ended 31st March 2026"],
    )

    reports = await history(session_factory, store, company_id)

    assert [(r.doc_id, r.version, r.fiscal_year) for r in reports] == [
        (corrected, 2, "FY2025-26")
    ]
