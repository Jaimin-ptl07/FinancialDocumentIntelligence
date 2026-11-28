"""`/api/v1/reports`: one Report's filing, and an Invitee confirming or
correcting it (S2.3).

A Report the issuer stage could not file confidently waits here with its
candidates and the reason. Any Invitee -- no roles -- may file it under a
Company or correct its Company or Fiscal year; every change records who and
when, and re-processing the Report never undoes it. Everything goes through
`fdi.pipeline.companies.CompanyRegistry`, never the tables."""

import structlog
from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from fdi.api.auth import CurrentUserDep
from fdi.api.deps import FactsDispatcherDep, SessionDep, StoreDep
from fdi.api.v1.companies import (
    ChangeListOut,
    ReportListOut,
    ReportOut,
    change_out,
    refused,
    report_out,
)
from fdi.pipeline.companies import CompanyCurationError, CompanyRegistry
from fdi.pipeline.dispatch import FactProjectionDispatcher
from fdi.platform.obs.logging import get_correlation_id

router = APIRouter(prefix="/reports", tags=["reports"])
log = structlog.get_logger(__name__)


def _reproject(dispatcher: FactProjectionDispatcher | None, doc_id: str) -> None:
    """Queue the Report's Financial fact re-projection (S5): its Observations
    carry the Company and Fiscal year it was filed under. The change itself is
    already committed, so a broker outage is logged, not failed -- the next
    `fdi-facts backfill` catches the Report up."""
    if dispatcher is None:
        return
    try:
        dispatcher.dispatch(doc_id, get_correlation_id() or "")
    except Exception as exc:  # noqa: BLE001 -- the correction stands either way
        log.warning("facts.reproject_dispatch_failed", doc_id=doc_id, error=repr(exc))


class CompanyLinkIn(BaseModel):
    company_id: str


class FiscalYearIn(BaseModel):
    # "FY2024-25" (also "2024-25", "FY2024-2025").
    fiscal_year: str


def _not_found(doc_id: str) -> HTTPException:
    return HTTPException(status_code=404, detail=f"report {doc_id} not found")


@router.get("/awaiting-confirmation", response_model=ReportListOut)
async def reports_awaiting_confirmation(session: SessionDep, store: StoreDep) -> ReportListOut:
    """Every Report waiting for an Invitee, newest upload first."""
    reports = await CompanyRegistry(session, store).reports_awaiting_confirmation()
    return ReportListOut(reports=[report_out(r) for r in reports])


@router.get("/{doc_id}", response_model=ReportOut)
async def get_report(doc_id: str, session: SessionDep, store: StoreDep) -> ReportOut:
    report = await CompanyRegistry(session, store).report(doc_id)
    if report is None:
        raise _not_found(doc_id)
    return report_out(report)


@router.put("/{doc_id}/company", response_model=ReportOut)
async def link_report(
    doc_id: str, body: CompanyLinkIn, session: SessionDep, store: StoreDep,
    invitee_id: CurrentUserDep, facts: FactsDispatcherDep,
) -> ReportOut:
    """Confirm the Report's Company, or correct it to another. 422 when the
    Company does not exist."""
    try:
        report = await CompanyRegistry(session, store).link_report(
            doc_id, company_id=body.company_id, invitee_id=invitee_id
        )
    except CompanyCurationError as exc:
        raise refused(exc) from exc
    if report is None:
        raise _not_found(doc_id)
    await session.commit()
    _reproject(facts, doc_id)
    return report_out(report)


@router.put("/{doc_id}/fiscal-year", response_model=ReportOut)
async def correct_fiscal_year(
    doc_id: str, body: FiscalYearIn, session: SessionDep, store: StoreDep,
    invitee_id: CurrentUserDep, facts: FactsDispatcherDep,
) -> ReportOut:
    """Correct the Report's Fiscal year; its period and its place in its
    Company's history follow. 422 for a label that is not one Fiscal year."""
    try:
        report = await CompanyRegistry(session, store).correct_fiscal_year(
            doc_id, fiscal_year=body.fiscal_year, invitee_id=invitee_id
        )
    except CompanyCurationError as exc:
        raise refused(exc) from exc
    if report is None:
        raise _not_found(doc_id)
    await session.commit()
    _reproject(facts, doc_id)
    return report_out(report)


@router.get("/{doc_id}/changes", response_model=ChangeListOut)
async def report_changes(doc_id: str, session: SessionDep, store: StoreDep) -> ChangeListOut:
    """Every manual change to the Report's Company or Fiscal year -- who made
    it and when, oldest first."""
    registry = CompanyRegistry(session, store)
    if await registry.report(doc_id) is None:
        raise _not_found(doc_id)
    return ChangeListOut(changes=[change_out(c) for c in await registry.report_changes(doc_id)])
