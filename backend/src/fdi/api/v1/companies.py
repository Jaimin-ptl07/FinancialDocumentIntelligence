"""`/api/v1/companies`: the Company registry as Invitees see it.

Every Invitee sees every Company and every Report filed under it -- these
are Public filings, so there is no per-user filtering here and none may be
added through Workspaces (#16). All reads go through
`fdi.pipeline.companies.CompanyRegistry`, never the tables."""

from datetime import date, datetime
from typing import Literal

from fastapi import APIRouter, HTTPException, Response, status
from pydantic import BaseModel

from fdi.api.auth import CurrentUserDep
from fdi.api.deps import SessionDep, StoreDep
from fdi.pipeline.companies import (
    ChangeView,
    CompanyConflict,
    CompanyCurationError,
    CompanyRegistry,
    CompanyView,
    IdentifierView,
    ReportView,
)

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
    fiscal_year_confidence: str | None
    fiscal_year_reason: str | None
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
        fiscal_year=r.fiscal_year, fiscal_year_confidence=r.fiscal_year_confidence,
        fiscal_year_reason=r.fiscal_year_reason,
        period_start=r.period_start, period_end=r.period_end,
        report_type=r.report_type, link_status=r.link_status, link_method=r.link_method,
        bases_present=list(r.bases_present), processing_status=r.processing_status,
    )


@router.get("", response_model=CompanyListOut)
async def list_companies(
    session: SessionDep, store: StoreDep, q: str | None = None
) -> CompanyListOut:
    """Every Company, or with `q` those found by name, alias, exchange
    identifier or CIN ("RIL", "Reliance Industries Ltd.")."""
    registry = CompanyRegistry(session, store)
    companies = (
        await registry.search(q) if q is not None and q.strip()
        else await registry.list_companies()
    )
    return CompanyListOut(companies=[_company_out(c) for c in companies])


# --- hand curation (S2.4): any Invitee; every change is logged with who/when --

IdentifierType = Literal["nse_symbol", "bse_code", "isin", "alias"]


class CompanyIn(BaseModel):
    name: str
    cin: str


class CompanyPatch(BaseModel):
    name: str | None = None
    cin: str | None = None


class IdentifierIn(BaseModel):
    type: IdentifierType
    value: str


class IdentifierOut(BaseModel):
    id: str
    type: str
    value: str


class IdentifierListOut(BaseModel):
    identifiers: list[IdentifierOut]


class ChangeOut(BaseModel):
    action: str
    invitee_id: str
    invitee_email: str
    at: datetime
    detail: dict[str, object]


class ChangeListOut(BaseModel):
    changes: list[ChangeOut]


def _identifier_out(i: IdentifierView) -> IdentifierOut:
    return IdentifierOut(id=i.id, type=i.kind, value=i.value)


def _change_out(c: ChangeView) -> ChangeOut:
    return ChangeOut(
        action=c.action, invitee_id=c.invitee_id, invitee_email=c.invitee_email,
        at=c.at, detail=c.detail,
    )


def _refused(exc: CompanyCurationError) -> HTTPException:
    if isinstance(exc, CompanyConflict):
        return HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc))
    return HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_CONTENT, detail=str(exc))


def _not_found(company_id: str) -> HTTPException:
    return HTTPException(status_code=404, detail=f"company {company_id} not found")


@router.post("", response_model=CompanyOut, status_code=status.HTTP_201_CREATED)
async def create_company(
    body: CompanyIn, session: SessionDep, store: StoreDep, invitee_id: CurrentUserDep
) -> CompanyOut:
    """Add a Company by hand. A Report ingested later that prints this CIN is
    filed under it. 409 when the CIN already has a Company."""
    try:
        company = await CompanyRegistry(session, store).create_company(
            name=body.name, cin=body.cin, invitee_id=invitee_id
        )
    except CompanyCurationError as exc:
        raise _refused(exc) from exc
    await session.commit()
    return _company_out(company)


@router.patch("/{company_id}", response_model=CompanyOut)
async def update_company(
    company_id: str, body: CompanyPatch, session: SessionDep, store: StoreDep,
    invitee_id: CurrentUserDep,
) -> CompanyOut:
    try:
        company = await CompanyRegistry(session, store).update_company(
            company_id, invitee_id=invitee_id, name=body.name, cin=body.cin
        )
    except CompanyCurationError as exc:
        raise _refused(exc) from exc
    if company is None:
        raise _not_found(company_id)
    await session.commit()
    return _company_out(company)


@router.get("/{company_id}/identifiers", response_model=IdentifierListOut)
async def list_identifiers(
    company_id: str, session: SessionDep, store: StoreDep
) -> IdentifierListOut:
    registry = CompanyRegistry(session, store)
    if await registry.get_company(company_id) is None:
        raise _not_found(company_id)
    identifiers = await registry.identifiers(company_id)
    return IdentifierListOut(identifiers=[_identifier_out(i) for i in identifiers])


@router.post(
    "/{company_id}/identifiers",
    response_model=IdentifierOut,
    status_code=status.HTTP_201_CREATED,
)
async def add_identifier(
    company_id: str, body: IdentifierIn, session: SessionDep, store: StoreDep,
    invitee_id: CurrentUserDep,
) -> IdentifierOut:
    """Add an NSE symbol, BSE code, ISIN or alias. 409 when any Company
    already has that (type, value)."""
    try:
        identifier = await CompanyRegistry(session, store).add_identifier(
            company_id, kind=body.type, value=body.value, invitee_id=invitee_id
        )
    except CompanyCurationError as exc:
        raise _refused(exc) from exc
    if identifier is None:
        raise _not_found(company_id)
    await session.commit()
    return _identifier_out(identifier)


@router.delete(
    "/{company_id}/identifiers/{identifier_id}", status_code=status.HTTP_204_NO_CONTENT
)
async def remove_identifier(
    company_id: str, identifier_id: str, session: SessionDep, store: StoreDep,
    invitee_id: CurrentUserDep,
) -> Response:
    removed = await CompanyRegistry(session, store).remove_identifier(
        company_id, identifier_id, invitee_id=invitee_id
    )
    if not removed:
        raise HTTPException(
            status_code=404,
            detail=f"company {company_id} has no identifier {identifier_id}",
        )
    await session.commit()
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.get("/{company_id}/changes", response_model=ChangeListOut)
async def list_changes(company_id: str, session: SessionDep, store: StoreDep) -> ChangeListOut:
    """Every manual change to the Company -- who made it and when."""
    registry = CompanyRegistry(session, store)
    if await registry.get_company(company_id) is None:
        raise _not_found(company_id)
    return ChangeListOut(changes=[_change_out(c) for c in await registry.changes(company_id)])


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
