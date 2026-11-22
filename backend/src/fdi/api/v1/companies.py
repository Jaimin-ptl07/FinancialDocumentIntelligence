"""`/api/v1/companies`: the Company registry as Invitees see it.

Every Invitee sees every Company and every Report filed under it -- these
are Public filings, so there is no per-user filtering here and none may be
added through Workspaces (#16). All reads go through
`fdi.pipeline.companies.CompanyRegistry`, never the tables."""

from datetime import date

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from fdi.api.deps import SessionDep, StoreDep
from fdi.pipeline.companies import CompanyRegistry, CompanyView, ReportView

router = APIRouter(prefix="/companies", tags=["companies"])


class CompanyOut(BaseModel):
    id: str
    cin: str
    name: str


class CompanyListOut(BaseModel):
    companies: list[CompanyOut]


class ReportOut(BaseModel):
    doc_id: str
    doc_key: str | None
    version: int | None
    filename: str | None
    fiscal_year: str | None
    period_start: date | None
    period_end: date | None
    report_type: str | None
    link_status: str | None
    link_method: str | None
    bases_present: list[str]
    processing_status: str


class ReportListOut(BaseModel):
    """Newest Fiscal year first."""

    reports: list[ReportOut]


def _company_out(c: CompanyView) -> CompanyOut:
    return CompanyOut(id=c.id, cin=c.cin, name=c.name)


def _report_out(r: ReportView) -> ReportOut:
    return ReportOut(
        doc_id=r.doc_id, doc_key=r.doc_key, version=r.version, filename=r.filename,
        fiscal_year=r.fiscal_year, period_start=r.period_start, period_end=r.period_end,
        report_type=r.report_type, link_status=r.link_status, link_method=r.link_method,
        bases_present=list(r.bases_present), processing_status=r.processing_status,
    )


@router.get("", response_model=CompanyListOut)
async def list_companies(session: SessionDep, store: StoreDep) -> CompanyListOut:
    companies = await CompanyRegistry(session, store).list_companies()
    return CompanyListOut(companies=[_company_out(c) for c in companies])


@router.get("/{company_id}", response_model=CompanyOut)
async def get_company(company_id: str, session: SessionDep, store: StoreDep) -> CompanyOut:
    company = await CompanyRegistry(session, store).get_company(company_id)
    if company is None:
        raise HTTPException(status_code=404, detail=f"company {company_id} not found")
    return _company_out(company)


@router.get("/{company_id}/reports", response_model=ReportListOut)
async def company_reports(
    company_id: str, session: SessionDep, store: StoreDep
) -> ReportListOut:
    registry = CompanyRegistry(session, store)
    if await registry.get_company(company_id) is None:
        raise HTTPException(status_code=404, detail=f"company {company_id} not found")
    reports = await registry.company_reports(company_id)
    return ReportListOut(reports=[_report_out(r) for r in reports])
