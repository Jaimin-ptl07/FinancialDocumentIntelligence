"""The company registry: the one interface to Companies and the Reports filed
under them (ADR-0003).

The worker (after the issuer stage), the backfill and the API all call
`CompanyRegistry`; none of them touch the company tables. Identification
itself is `fdi.core.issuer` (pure) materialised by the issuer stage; this
module only acts on its verdict:

* a confident CIN that is already known -> file the Report under it;
* a confident CIN that is new -> create the Company, then file the Report;
* anything else (no CIN, no clear winner, no issuer artifact) -> leave the
  Report under NO Company and say why. A Company is never created on a
  guess, nor from a name alone (design §6.1).

Resolving also records the Report's metadata from the same artifact (S2.2):
its Fiscal year and period, report type, link status and link method (`cin`).
`linked` means filed on a confident CIN (or by an Invitee);
`awaiting_confirmation` means the Report prints something about its issuer --
CINs with no clear winner, or company names but no CIN -- and waits, with
those candidates and the reason, for an Invitee; `unidentified` means it
prints neither. Neither of the last two is ever under a Company unless an
Invitee put it there. Statement bases are never stored: `company_reports`
reads them from the basis segmentation each time ("bases present").

An Invitee confirms or corrects a Report's Company (`link_report`) and
Fiscal year (`correct_fiscal_year`) here too (S2.3). Either is marked
`confirmed_by_invitee` and logged with who and when (append-only), and
resolving the Report again -- re-processing, backfill -- never overwrites it.

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
from fdi.core.issuer import fiscal_year_period
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
    ReportLinkChange,
    StageStatus,
)
from fdi.platform.db.repository import CompanyRepository, RegistryRepository
from fdi.platform.storage.artifact_store import ArtifactStore

REPORT_TYPE_ANNUAL = "annual_report"

LINKED = "linked"
AWAITING_CONFIRMATION = "awaiting_confirmation"
UNIDENTIFIED = "unidentified"
LINK_METHOD_CIN = "cin"
# An Invitee confirmed or corrected it (S2.3). Used for both the Company link
# (`link_method`) and the Fiscal year (`fiscal_year_method`); re-resolving a
# Report never overwrites what an Invitee set.
LINK_METHOD_INVITEE = "confirmed_by_invitee"

# The stage whose completion makes a Report searchable by /ask.
_RESEARCH_READY_STAGE = "index"


@dataclass(frozen=True)
class CinCandidate:
    """A CIN printed in the Report, with the company name printed beside it
    and the page."""

    cin: str
    name: str | None
    page: int | None


@dataclass(frozen=True)
class LinkCandidates:
    """What an Invitee chooses between for a Report awaiting confirmation:
    the CINs it prints, the company names it prints, and the Companies
    already in the registry that one of those CINs or names belongs to."""

    cins: "tuple[CinCandidate, ...]" = ()
    names: tuple[str, ...] = ()
    companies: "tuple[CompanyView, ...]" = ()


@dataclass(frozen=True)
class ReportLink:
    """What resolving one Report did. `company_id` is None when the Report
    was left unfiled; `reason` says why either way. `link_status` is None only
    when the issuer stage has not run. `candidates` are set while the Report
    awaits confirmation."""

    doc_id: str
    company_id: str | None
    cin: str | None
    created_company: bool
    reason: str
    link_status: str | None = None
    fiscal_year: str | None = None
    candidates: LinkCandidates | None = None


# --- hand curation and finding a Company (S2.4) ------------------------------


class CompanyCurationError(Exception):
    """A manual change the registry refused; the message says why."""


class InvalidCompanyInput(CompanyCurationError):
    """A malformed CIN, name or identifier."""


class CompanyConflict(CompanyCurationError):
    """The CIN or (type, value) identifier already belongs to a Company."""


class UnknownCompany(CompanyCurationError):
    """A Report cannot be filed under a Company that does not exist."""


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


def _report_change_view(change: ReportLinkChange, email: str) -> ChangeView:
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
    # How sure the issuer stage is of `fiscal_year` ("high", "low" when the
    # Report contradicts itself, "none" when no year is printed) and why --
    # read from the issuer artifact, so an open Fiscal year is explained.
    fiscal_year_confidence: str | None
    fiscal_year_reason: str | None
    period_start: date | None
    period_end: date | None
    report_type: str | None
    link_status: str | None
    link_method: str | None
    bases_present: tuple[str, ...]
    processing_status: str
    # S2.3: the Company it is filed under (None while awaiting confirmation
    # or unidentified, unless an Invitee filed it), why the issuer stage
    # could or could not file it, and -- only while awaiting confirmation --
    # what an Invitee chooses between.
    company_id: str | None = None
    link_reason: str | None = None
    candidates: LinkCandidates | None = None
    # How the Fiscal year was set: None (the issuer stage) or
    # "confirmed_by_invitee"; and the latest manual change, who and when.
    fiscal_year_method: str | None = None
    last_change: "ChangeView | None" = None


def _str_or_none(value: object) -> str | None:
    return value if isinstance(value, str) else None


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
    """Linked only on a confident CIN. Anything the Report does print about
    its issuer -- CINs with no clear winner, or company names with no CIN --
    awaits an Invitee; a Report printing neither is unidentified."""
    if isinstance(issuer.get("cin"), str) and isinstance(issuer.get("issuer_name"), str):
        return LINKED
    if issuer.get("cin_candidates") or issuer.get("name_candidates"):
        return AWAITING_CONFIRMATION
    return UNIDENTIFIED


def _mappings(value: object) -> list[Mapping[str, object]]:
    return [v for v in value if isinstance(v, Mapping)] if isinstance(value, list) else []


def _cin_candidates(issuer: Mapping[str, object]) -> tuple[CinCandidate, ...]:
    out: list[CinCandidate] = []
    for c in _mappings(issuer.get("cin_candidates")):
        cin = c.get("cin")
        if not isinstance(cin, str):
            continue
        evidence = c.get("evidence")
        page = evidence.get("page") if isinstance(evidence, Mapping) else None
        out.append(
            CinCandidate(
                cin=cin, name=_str_or_none(c.get("name")),
                page=page if isinstance(page, int) and not isinstance(page, bool) else None,
            )
        )
    return tuple(out)


def _name_candidates(issuer: Mapping[str, object]) -> tuple[str, ...]:
    names = (_str_or_none(n.get("name")) for n in _mappings(issuer.get("name_candidates")))
    return tuple(n for n in names if n is not None)


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
        # What an Invitee set is theirs: the writes below skip a Fiscal year
        # or a link made `confirmed_by_invitee`, in the UPDATE itself, so a
        # resolution racing an Invitee's confirm cannot undo it either.
        await self._companies.set_report_coverage(
            doc_id,
            fiscal_year=_str_or_none(issuer.get("fiscal_year")),
            period_start=_iso_date(issuer.get("period_start")),
            period_end=_iso_date(issuer.get("period_end")),
            report_type=REPORT_TYPE_ANNUAL,
            fiscal_year_method=None,
            keep_method=LINK_METHOD_INVITEE,
        )
        doc = await self._companies.report(doc_id)
        if doc is None:
            return ReportLink(
                doc_id=doc_id, company_id=None, cin=None, created_company=False,
                reason="there is no such Report",
            )
        fiscal_year = doc.fiscal_year
        if doc.link_method == LINK_METHOD_INVITEE:
            return self._kept(doc)
        reason = str(issuer.get("reason", ""))
        cin = issuer.get("cin")
        name = issuer.get("issuer_name")
        if not isinstance(cin, str) or not isinstance(name, str):
            # No confident issuer: filed under no Company, whatever an
            # earlier resolution filed it under.
            if not await self._companies.set_report_link(
                doc_id, company_id=None, link_status=link_status, link_method=None,
                keep_method=LINK_METHOD_INVITEE,
            ):
                return await self._kept_now(doc_id)
            return ReportLink(
                doc_id=doc_id, company_id=None, cin=None, created_company=False,
                reason=reason, link_status=link_status, fiscal_year=fiscal_year,
                candidates=(
                    await self._candidates(issuer)
                    if link_status == AWAITING_CONFIRMATION else None
                ),
            )
        company, created = await self._companies.get_or_create_by_cin(cin=cin, name=name)
        if not await self._companies.set_report_link(
            doc_id, company_id=company.id, link_status=LINKED, link_method=LINK_METHOD_CIN,
            keep_method=LINK_METHOD_INVITEE,
        ):
            return await self._kept_now(doc_id)
        return ReportLink(
            doc_id=doc_id, company_id=company.id, cin=cin, created_company=created,
            reason=reason, link_status=link_status, fiscal_year=fiscal_year,
        )

    @staticmethod
    def _kept(doc: Document) -> ReportLink:
        return ReportLink(
            doc_id=doc.id, company_id=doc.company_id, cin=None, created_company=False,
            reason="filed by an Invitee; resolving the Report again leaves it there",
            link_status=doc.link_status, fiscal_year=doc.fiscal_year,
        )

    async def _kept_now(self, doc_id: str) -> ReportLink:
        """The link write was skipped: an Invitee filed the Report while this
        resolution ran (or the Report has since gone)."""
        doc = await self._companies.report(doc_id)
        if doc is None:
            return ReportLink(
                doc_id=doc_id, company_id=None, cin=None, created_company=False,
                reason="there is no such Report",
            )
        return self._kept(doc)

    async def _candidates(self, issuer: Mapping[str, object]) -> LinkCandidates:
        """The Report's printed CINs and names, and the registry's Companies
        that one of them belongs to -- by CIN, or by name or alias, spelling
        aside. Suggestions only: nothing here files the Report."""
        cins = _cin_candidates(issuer)
        names = _name_candidates(issuer)
        printed_cins = {c.cin for c in cins}
        printed_names = [*names, *(c.name for c in cins if c.name is not None)]
        companies = tuple(
            _company_view(company)
            for company, known_as in await self._names()
            if company.cin in printed_cins
            or any(is_same_name(p, k) for p in printed_names for k in known_as)
        )
        return LinkCandidates(cins=cins, names=names, companies=companies)

    async def report(self, doc_id: str) -> ReportView | None:
        """One Report as an Invitee sees it; None when there is no such
        Report."""
        doc = await self._companies.report(doc_id)
        return await self._report_view(doc) if doc is not None else None

    async def reports_awaiting_confirmation(self) -> list[ReportView]:
        """Every Report waiting for an Invitee, newest upload first."""
        return [
            await self._report_view(d)
            for d in await self._companies.reports_by_link_status(AWAITING_CONFIRMATION)
        ]

    # --- an Invitee confirms or corrects a Report (S2.3) --------------------

    async def link_report(
        self, doc_id: str, *, company_id: str, invitee_id: str
    ) -> ReportView | None:
        """File a Report under `company_id` by an Invitee's word: confirming
        it (it was under no Company, or already under this one) or correcting
        it (it was under another). Its link method becomes
        `confirmed_by_invitee`, which re-resolving the Report never
        overwrites. None when there is no such Report; raises
        `UnknownCompany` when there is no such Company."""
        doc = await self._companies.report(doc_id)
        if doc is None:
            return None
        if await self._companies.get(company_id) is None:
            raise UnknownCompany(f"no Company {company_id}")
        previous = doc.company_id
        await self._companies.set_report_link(
            doc_id, company_id=company_id, link_status=LINKED, link_method=LINK_METHOD_INVITEE
        )
        detail: dict[str, object]
        if previous is None or previous == company_id:
            action, detail = "confirm", {"company_id": company_id}
        else:
            action, detail = "correct_company", {"company_id": [previous, company_id]}
        await self._companies.record_report_change(
            doc_id, invitee_id=invitee_id, action=action, detail=detail
        )
        return await self.report(doc_id)

    async def correct_fiscal_year(
        self, doc_id: str, *, fiscal_year: str, invitee_id: str
    ) -> ReportView | None:
        """Set a Report's Fiscal year by an Invitee's word ("FY2024-25"): its
        period follows (April 2024 - March 2025), so does its place in its
        Company's history, and re-resolving the Report never overwrites it.
        None when there is no such Report; raises `InvalidCompanyInput` for a
        label that is not one Fiscal year."""
        period = fiscal_year_period(fiscal_year)
        if period is None:
            raise InvalidCompanyInput(f"{fiscal_year!r} is not a Fiscal year like FY2024-25")
        doc = await self._companies.report(doc_id)
        if doc is None:
            return None
        label, start, end = period
        previous = doc.fiscal_year
        await self._companies.set_report_coverage(
            doc_id, fiscal_year=label, period_start=start, period_end=end,
            report_type=doc.report_type or REPORT_TYPE_ANNUAL,
            fiscal_year_method=LINK_METHOD_INVITEE,
        )
        await self._companies.record_report_change(
            doc_id, invitee_id=invitee_id, action="correct_fiscal_year",
            detail={"fiscal_year": [previous, label]},
        )
        return await self.report(doc_id)

    async def report_changes(self, doc_id: str) -> list[ChangeView]:
        """Every manual change to the Report's link, oldest first."""
        return [
            _report_change_view(c, email)
            for c, email in await self._companies.report_changes(doc_id)
        ]

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
        issuer = await read_artifact(registry, self._store, doc.id, IssuerStage.name) or {}
        latest = {stage: run.status for stage, run in (await registry.latest_runs(doc.id)).items()}
        changes = await self._companies.report_changes(doc.id)
        manual_link = doc.link_method == LINK_METHOD_INVITEE
        return ReportView(
            company_id=doc.company_id,
            link_reason=None if manual_link else _str_or_none(issuer.get("reason")),
            candidates=(
                await self._candidates(issuer)
                if doc.link_status == AWAITING_CONFIRMATION and not manual_link else None
            ),
            fiscal_year_method=doc.fiscal_year_method,
            last_change=_report_change_view(*changes[-1]) if changes else None,
            doc_id=doc.id, doc_key=doc.doc_key, version=doc.version, filename=doc.filename,
            fiscal_year=doc.fiscal_year,
            fiscal_year_confidence=_str_or_none(issuer.get("fiscal_year_confidence")),
            fiscal_year_reason=_str_or_none(issuer.get("fiscal_year_reason")),
            period_start=doc.period_start,
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
