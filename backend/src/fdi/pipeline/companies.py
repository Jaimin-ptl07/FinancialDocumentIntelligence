"""The company registry: the one interface to Companies and the Reports filed
under them (ADR-0003).

The worker (after the issuer stage), the backfill and the API all call
`CompanyRegistry`; none of them touch the company tables. Identification
itself is `fdi.core.issuer` (pure) materialised by the issuer stage; this
module only acts on its verdict:

* a confident CIN that is already known -> file the Report under it;
* a confident CIN that is new -> create the Company, then file the Report;
* anything else (no CIN, no clear winner, no issuer artifact) -> leave the
  Report unfiled and say why. A Company is never created on a guess.

Resolving also records the Report's metadata from the same artifact (S2.2):
its Fiscal year and period, report type, link status (`linked`,
`awaiting_confirmation` when CINs are printed but none is clearly the
issuer's, `unidentified` when none is printed) and link method (`cin`).
Statement bases are never stored: `company_reports` reads them from the
basis segmentation each time ("bases present").

A pipeline-layer service, the same shape as `fdi.pipeline.crossdoc`:
pipeline -> platform -> core.
"""

import uuid
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import date

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from fdi.core.basis import bases_present
from fdi.pipeline.crossdoc import read_artifact
from fdi.pipeline.registry import REGISTRY
from fdi.pipeline.resolver import DagResolver
from fdi.pipeline.run import run_stage
from fdi.pipeline.stages.basis import BasisStage
from fdi.pipeline.stages.issuer import ISSUER_STAGE, IssuerStage
from fdi.platform.db.models import Company, Document, StageStatus
from fdi.platform.db.repository import CompanyRepository, RegistryRepository
from fdi.platform.storage.artifact_store import ArtifactStore

REPORT_TYPE_ANNUAL = "annual_report"

LINKED = "linked"
AWAITING_CONFIRMATION = "awaiting_confirmation"
UNIDENTIFIED = "unidentified"
LINK_METHOD_CIN = "cin"

# The stage whose completion makes a Report searchable by /ask.
_RESEARCH_READY_STAGE = "index"


@dataclass(frozen=True)
class ReportLink:
    """What resolving one Report did. `company_id` is None when the Report
    was left unfiled; `reason` says why either way. `link_status` is None only
    when the issuer stage has not run."""

    doc_id: str
    company_id: str | None
    cin: str | None
    created_company: bool
    reason: str
    link_status: str | None = None
    fiscal_year: str | None = None


@dataclass(frozen=True)
class CompanyView:
    id: str
    cin: str
    name: str


@dataclass(frozen=True)
class ReportView:
    """A Report as an Invitee sees it: what it covers, how it was filed, which
    statement bases it contains (read from the basis segmentation, never
    stored) and whether it is ready to research (`processing_status`:
    "ready" once searchable, else "failed" if a stage failed, else
    "processing")."""

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
    bases_present: tuple[str, ...]
    processing_status: str


def _company_view(company: Company) -> CompanyView:
    return CompanyView(id=company.id, cin=company.cin, name=company.name)


def processing_status(latest_run_status: Mapping[str, str]) -> str:
    """A Report's readiness from the status of its latest run per stage: ready
    to research once it is searchable; otherwise failed if any stage failed;
    otherwise still processing."""
    if latest_run_status.get(_RESEARCH_READY_STAGE) == StageStatus.DONE:
        return "ready"
    if StageStatus.FAILED in latest_run_status.values():
        return "failed"
    return "processing"


def _iso_date(value: object) -> date | None:
    if not isinstance(value, str):
        return None
    try:
        return date.fromisoformat(value)
    except ValueError:
        return None


def _link_status(issuer: Mapping[str, object]) -> str:
    if isinstance(issuer.get("cin"), str) and isinstance(issuer.get("issuer_name"), str):
        return LINKED
    if issuer.get("cin_candidates"):
        return AWAITING_CONFIRMATION
    return UNIDENTIFIED


class CompanyRegistry:
    """The caller owns the transaction: `resolve_report` flushes, the caller
    commits."""

    def __init__(self, session: AsyncSession, store: ArtifactStore) -> None:
        self._s = session
        self._store = store
        self._companies = CompanyRepository(session)

    async def resolve_report(self, doc_id: str) -> ReportLink:
        """File one Report under its Company from its issuer artifact.
        Idempotent: resolving an already-filed Report files it again under
        the same Company."""
        issuer = await read_artifact(RegistryRepository(self._s), self._store, doc_id, ISSUER_STAGE)
        if issuer is None:
            return ReportLink(
                doc_id=doc_id, company_id=None, cin=None, created_company=False,
                reason="the issuer stage has not run",
            )
        link_status = _link_status(issuer)
        fiscal_year = issuer.get("fiscal_year")
        fiscal_year = fiscal_year if isinstance(fiscal_year, str) else None
        await self._companies.set_report_metadata(
            doc_id,
            fiscal_year=fiscal_year,
            period_start=_iso_date(issuer.get("period_start")),
            period_end=_iso_date(issuer.get("period_end")),
            report_type=REPORT_TYPE_ANNUAL,
            link_status=link_status,
            link_method=LINK_METHOD_CIN if link_status == LINKED else None,
        )
        cin = issuer.get("cin")
        name = issuer.get("issuer_name")
        if not isinstance(cin, str) or not isinstance(name, str):
            return ReportLink(
                doc_id=doc_id, company_id=None, cin=None, created_company=False,
                reason=str(issuer.get("reason", "")),
                link_status=link_status, fiscal_year=fiscal_year,
            )
        company, created = await self._companies.get_or_create_by_cin(cin=cin, name=name)
        await self._companies.link_document(doc_id, company.id)
        return ReportLink(
            doc_id=doc_id, company_id=company.id, cin=cin, created_company=created,
            reason=str(issuer.get("reason", "")),
            link_status=link_status, fiscal_year=fiscal_year,
        )

    async def list_companies(self) -> list[CompanyView]:
        return [_company_view(c) for c in await self._companies.all()]

    async def get_company(self, company_id: str) -> CompanyView | None:
        company = await self._companies.get(company_id)
        return _company_view(company) if company is not None else None

    async def company_reports(self, company_id: str) -> list[ReportView]:
        """A Company's Reports, newest Fiscal year first -- the order is
        `CompanyRepository.latest_documents`'s, the only place it is decided."""
        return [
            await self._report_view(d) for d in await self._companies.latest_documents(company_id)
        ]

    async def _report_view(self, doc: Document) -> ReportView:
        registry = RegistryRepository(self._s)
        basis = await read_artifact(registry, self._store, doc.id, BasisStage.name)
        latest = {stage: run.status for stage, run in (await registry.latest_runs(doc.id)).items()}
        return ReportView(
            doc_id=doc.id, doc_key=doc.doc_key, version=doc.version, filename=doc.filename,
            fiscal_year=doc.fiscal_year, period_start=doc.period_start,
            period_end=doc.period_end, report_type=doc.report_type,
            link_status=doc.link_status, link_method=doc.link_method,
            bases_present=tuple(bases_present(basis)) if basis is not None else (),
            processing_status=processing_status(latest),
        )


async def resolve_and_commit(
    *, session_factory: async_sessionmaker[AsyncSession], store: ArtifactStore, doc_id: str
) -> ReportLink:
    """`resolve_report` in its own transaction -- what the worker runs once the
    issuer stage completes, and what the backfill runs per Report."""
    async with session_factory() as s:
        link = await CompanyRegistry(s, store).resolve_report(doc_id)
        await s.commit()
    return link


async def backfill_companies(
    *, session_factory: async_sessionmaker[AsyncSession], store: ArtifactStore
) -> list[ReportLink]:
    """File every already-ingested Report by the same path a new upload takes:
    run the issuer stage (a cache hit when it already ran), then resolve the
    Report against the registry. A Report whose text stages have not
    completed is skipped -- it will be filed when its own chain reaches the
    issuer stage. Each Report runs and commits on its own: one that fails is
    reported as unfiled with the error, and the rest are still filed."""
    async with session_factory() as s:
        repo = RegistryRepository(s)
        ready: list[str] = []
        for doc in await repo.list_documents():
            runs = [await repo.latest_completed_run(doc.id, dep) for dep in IssuerStage.deps]
            if all(run is not None for run in runs):
                ready.append(doc.id)
    resolver = DagResolver(REGISTRY.all())
    links: list[ReportLink] = []
    for doc_id in ready:
        try:
            await run_stage(
                doc_id=doc_id, stage_name=ISSUER_STAGE, run_id=str(uuid.uuid4()),
                correlation_id=None, session_factory=session_factory, store=store,
                registry=REGISTRY, resolver=resolver,
            )
            links.append(
                await resolve_and_commit(
                    session_factory=session_factory, store=store, doc_id=doc_id
                )
            )
        except Exception as exc:  # noqa: BLE001 -- one Report's failure is its own outcome
            links.append(
                ReportLink(
                    doc_id=doc_id, company_id=None, cin=None, created_company=False,
                    reason=f"backfill failed: {exc!r}",
                )
            )
    return links
