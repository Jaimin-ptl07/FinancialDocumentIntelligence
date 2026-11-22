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
from dataclasses import dataclass, field
from datetime import date, datetime

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from fdi.core.basis import bases_present
from fdi.core.company_names import (
    IDENTIFIER_KINDS,
    identifier_key,
    is_same_name,
    matches_query,
    normalize_cin,
    normalize_identifier,
)
from fdi.pipeline.crossdoc import read_artifact
from fdi.pipeline.registry import REGISTRY
from fdi.pipeline.resolver import DagResolver
from fdi.pipeline.run import run_stage
from fdi.pipeline.stages.basis import BasisStage
from fdi.pipeline.stages.issuer import ISSUER_STAGE, IssuerStage
from fdi.platform.db.models import (
    Company,
    CompanyChange,
    CompanyIdentifier,
    Document,
    StageStatus,
)
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


# --- hand curation and finding a Company (S2.4) ------------------------------


class CompanyCurationError(Exception):
    """A manual change the registry refused; the message says why."""


class InvalidCompanyInput(CompanyCurationError):
    """A malformed CIN, name or identifier."""


class CompanyConflict(CompanyCurationError):
    """The CIN or (type, value) identifier already belongs to a Company."""


@dataclass(frozen=True)
class IdentifierView:
    id: str
    kind: str
    value: str


@dataclass(frozen=True)
class ChangeView:
    """One manual change: what, by which Invitee, when."""

    action: str
    invitee_id: str
    invitee_email: str
    at: datetime
    detail: dict[str, object]


@dataclass(frozen=True)
class CompanyResolution:
    """What a Company name or alias resolves to. `company` is set only when
    exactly one Company carries that name, alias, identifier or CIN; its
    Reports are then listed as `CompanyRegistry.company_reports` lists them
    (by Fiscal year). Otherwise `company` is None and `candidates` are the
    Companies the caller must choose between (every exact match, or, with
    none, what a search finds)."""

    query: str
    company: "CompanyView | None"
    reports: "list[ReportView]" = field(default_factory=list)
    candidates: "list[CompanyView]" = field(default_factory=list)


def _identifier_view(row: CompanyIdentifier) -> IdentifierView:
    return IdentifierView(id=row.id, kind=row.kind, value=row.value)


def _change_view(change: CompanyChange, email: str) -> ChangeView:
    return ChangeView(
        action=change.action, invitee_id=change.invitee_id, invitee_email=email,
        at=change.at, detail=dict(change.detail),
    )


def _clean_name(name: str) -> str:
    cleaned = " ".join(name.split())
    if not cleaned:
        raise InvalidCompanyInput("a Company needs a name")
    return cleaned


def _clean_cin(cin: str) -> str:
    canonical = normalize_cin(cin)
    if canonical is None:
        raise InvalidCompanyInput(f"{cin!r} is not a 21-character CIN")
    return canonical


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

    # --- hand curation (S2.4): every change records the acting Invitee -----

    async def create_company(self, *, name: str, cin: str, invitee_id: str) -> CompanyView:
        """A Company entered by hand, ready for its Reports: one later
        ingested that prints this CIN is filed under it, not under a new
        Company. Raises `CompanyConflict` when the CIN is taken."""
        clean_name, clean_cin = _clean_name(name), _clean_cin(cin)
        company = await self._companies.create(cin=clean_cin, name=clean_name)
        if company is None:
            raise CompanyConflict(f"a Company with CIN {clean_cin} already exists")
        await self._companies.record_change(
            company.id, invitee_id=invitee_id, action="create",
            detail={"name": clean_name, "cin": clean_cin},
        )
        return _company_view(company)

    async def update_company(
        self, company_id: str, *, invitee_id: str, name: str | None = None,
        cin: str | None = None,
    ) -> CompanyView | None:
        """Rename a Company or correct its CIN; None when it does not exist.
        Raises `CompanyConflict` when the CIN belongs to another Company."""
        company = await self._companies.get(company_id)
        if company is None:
            return None
        new_name = company.name if name is None else _clean_name(name)
        new_cin = company.cin if cin is None else _clean_cin(cin)
        detail: dict[str, object] = {
            label: [old, new]
            for label, old, new in (
                ("name", company.name, new_name), ("cin", company.cin, new_cin)
            )
            if old != new
        }
        if not detail:
            return _company_view(company)
        if not await self._companies.update(company, cin=new_cin, name=new_name):
            raise CompanyConflict(f"a Company with CIN {new_cin} already exists")
        await self._companies.record_change(
            company_id, invitee_id=invitee_id, action="update", detail=detail
        )
        return _company_view(company)

    async def identifiers(self, company_id: str) -> list[IdentifierView]:
        return [_identifier_view(i) for i in await self._companies.identifiers(company_id)]

    async def add_identifier(
        self, company_id: str, *, kind: str, value: str, invitee_id: str
    ) -> IdentifierView | None:
        """Add an NSE symbol, BSE code, ISIN or alias; None when the Company
        does not exist. Raises `CompanyConflict` when any Company already has
        that (type, value) -- spelling aside, for aliases."""
        if kind not in IDENTIFIER_KINDS:
            raise InvalidCompanyInput(f"{kind!r} is not one of {', '.join(IDENTIFIER_KINDS)}")
        canonical, key = normalize_identifier(kind, value), identifier_key(kind, value)
        if canonical is None or key is None:
            raise InvalidCompanyInput(f"{value!r} is not a valid {kind}")
        if await self._companies.get(company_id) is None:
            return None
        row = await self._companies.add_identifier(
            company_id, kind=kind, value=canonical, key=key
        )
        if row is None:
            raise CompanyConflict(f"the {kind} {canonical!r} already belongs to a Company")
        await self._companies.record_change(
            company_id, invitee_id=invitee_id, action="add_identifier",
            detail={"type": kind, "value": canonical},
        )
        return _identifier_view(row)

    async def remove_identifier(
        self, company_id: str, identifier_id: str, *, invitee_id: str
    ) -> bool:
        """False when the Company has no such identifier."""
        row = await self._companies.remove_identifier(company_id, identifier_id)
        if row is None:
            return False
        await self._companies.record_change(
            company_id, invitee_id=invitee_id, action="remove_identifier",
            detail={"type": row.kind, "value": row.value},
        )
        return True

    async def changes(self, company_id: str) -> list[ChangeView]:
        """Every manual change to the Company, oldest first."""
        return [_change_view(c, email) for c, email in await self._companies.changes(company_id)]

    async def _names(self) -> list[tuple[Company, list[str]]]:
        """Each Company with every name it answers to: its registered name,
        its CIN and each identifier's value. The registry holds one row per
        issuer, so matching runs here through `fdi.core.company_names` rather
        than as SQL re-implementing its spelling rules."""
        by_company: dict[str, list[str]] = {}
        for ident in await self._companies.identifiers():
            by_company.setdefault(ident.company_id, []).append(ident.value)
        return [
            (c, [c.name, c.cin, *by_company.get(c.id, [])]) for c in await self._companies.all()
        ]

    async def search(self, query: str) -> list[CompanyView]:
        """Companies whose name, CIN, alias or exchange identifier contains
        `query` as whole words -- exact matches first, then by name."""
        hits = [
            (not any(is_same_name(query, n) for n in names), company)
            for company, names in await self._names()
            if any(matches_query(query, n) for n in names)
        ]
        hits.sort(key=lambda hit: hit[0])  # stable: name order within each group
        return [_company_view(company) for _, company in hits]

    async def resolve_company(self, query: str) -> CompanyResolution:
        """Resolve a Company name, alias, identifier or CIN to the one Company
        it names, with that Company's Reports by Fiscal year -- the call
        agents (S4-S8) scope a question with, so it never depends on a
        `doc_key`. Exact names only, spelling aside: "Reliance" alone is a
        search, not a resolution. The Reports are `company_reports`' own, so
        its ordering is the only one there is."""
        exact = [
            company for company, names in await self._names()
            if any(is_same_name(query, n) for n in names)
        ]
        if len(exact) == 1:
            company = _company_view(exact[0])
            return CompanyResolution(
                query=query, company=company,
                reports=await self.company_reports(company.id), candidates=[company],
            )
        candidates = [_company_view(c) for c in exact] if exact else await self.search(query)
        return CompanyResolution(query=query, company=None, candidates=candidates)

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
