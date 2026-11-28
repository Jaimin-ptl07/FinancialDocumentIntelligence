"""The Retrieval service (S4): one retrieval surface for agents, the
extractor and the API, with one result shape -- Evidence.

Two operations, everything else internal:

    retrieve(request) -> EvidencePackage
    inspect(evidence_id) -> InspectedEvidence | None

A request names explicit scope (Company, Reports, Report versions, Fiscal
years, statement bases, section types, Evidence kinds) and the paths to run,
or `auto`. Four paths:

* structured     -- financial facts by Company x concept x Fiscal year x basis,
                    read from each Report's financial object graph
                    (`fog_objects` + `fog_mapping`; S5 swaps the backing store
                    behind the same request without changing callers).
* hybrid         -- today's dense + BM25 channels fused by RRF, unchanged in
                    ranking; every scope filter is applied inside the vector
                    store (fdi.pipeline.retrieval).
* structural     -- whole statements, notes, tables, pages and sections, and a
                    statement line -> the note it cites (FOG REFERENCES).
* cross_document -- the same request fanned out over every scoped Report,
                    grouped by Fiscal year, each Report matched / no_match /
                    not_processed.

Scope is resolved HERE, never taken from a model: Company and Fiscal year
resolve to Document versions through Postgres at query time (the S2
registry); basis/kind/section come from closed enums; the vector-store filter
is built only from resolved ids and enum literals
(fdi.core.evidence.scope_filter_expression). Workspaces are not a boundary
and every Invitee may read every Report, so scope is about correctness, not
authorization.

Degradation: each path reports ok / degraded(reason) / skipped(reason) in the
package and `retrieve` never raises for a store failure -- a Milvus outage
degrades `hybrid` while `structured` and `structural` still answer (design
§13.4). An invalid REQUEST is the caller's error and fails validation when the
request is built, before any I/O.

Natural-language understanding (which Company, which year) is the agent's job
(S7); this service takes explicit scope.
"""

from __future__ import annotations

import contextlib
from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass, field, replace
from typing import Annotated, Literal, cast, get_args

import structlog
from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    StringConstraints,
    ValidationError,
    model_validator,
)
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession

from fdi.core import evidence_context as ctxv
from fdi.core.basis import page_basis
from fdi.core.evidence import (
    Basis,
    Evidence,
    EvidenceKind,
    EvidenceRef,
    FiscalYearGroup,
    ReportMeta,
    ReportOutcome,
    RetrievalPath,
    cell_ref,
    evidence_body,
    evidence_from_hit,
    fact_ref,
    group_by_fiscal_year,
    legacy_hit,
    merge_evidence,
    page_ref,
    parse_evidence_id,
    passage_ref,
    ref_for_fog_node,
    report_status,
    table_ref,
)
from fdi.core.fog_reason import fact_claims
from fdi.core.semantic import SectionType
from fdi.core.statements import STATEMENT_ANCHORS
from fdi.pipeline.companies import CompanyRegistry
from fdi.pipeline.crossdoc import read_artifact
from fdi.pipeline.embeddings import EmbeddingClient
from fdi.pipeline.retrieval import bm25_search, hybrid_search, vector_search
from fdi.pipeline.vectorstore import VectorStore
from fdi.platform.db.models import Document
from fdi.platform.db.repository import DocumentRepository, RegistryRepository
from fdi.platform.storage.artifact_store import ArtifactStore

log = structlog.get_logger(__name__)

# ---------------------------------------------------------------------------
# Request
# ---------------------------------------------------------------------------

_ID_PATTERN = r"^[A-Za-z0-9_-]{1,64}$"
DocId = Annotated[str, StringConstraints(pattern=_ID_PATTERN)]
DocKey = Annotated[str, StringConstraints(min_length=1, max_length=256)]
# "FY2025-26" (an April-March year) or "FY2025" (a calendar year) -- the only
# labels the issuer stage and an Invitee's correction ever store.
FiscalYear = Annotated[str, StringConstraints(pattern=r"^FY\d{4}(-\d{2})?$")]
StatementName = Literal["balance_sheet", "profit_and_loss", "cash_flow"]
assert set(get_args(StatementName)) == set(STATEMENT_ANCHORS)

Channel = Literal["dense", "bm25"]

# The most Evidence one hybrid request returns. The pre-S4 routes took any k;
# `legacy_search` clamps to this rather than refuse.
_MAX_K = 200


class Scope(BaseModel):
    """What a request may see. Every dimension narrows; none widens.

    Without `all_versions`, every Report contributes its LATEST version only
    (a superseded upload never doubles up); name a version explicitly with
    one `doc_key` + `version`, or with `doc_ids`. `bases` is exact:
    `["consolidated"]` excludes text outside every basis region, which is
    `unknown` -- ask for `["consolidated", "unknown"]` to keep it."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    company_id: DocId | None = None
    doc_keys: tuple[DocKey, ...] = Field(default=(), max_length=50)
    doc_ids: tuple[DocId, ...] = Field(default=(), max_length=1000)
    version: int | None = Field(default=None, ge=1)
    fiscal_years: tuple[FiscalYear, ...] = Field(default=(), max_length=30)
    bases: tuple[Basis, ...] = ()
    section_types: tuple[SectionType, ...] = ()
    kinds: tuple[EvidenceKind, ...] = ()
    all_versions: bool = False

    @model_validator(mode="after")
    def _version_names_one_report(self) -> Scope:
        if self.version is not None and len(self.doc_keys) != 1:
            raise ValueError("`version` needs exactly one doc_key")
        return self

    @property
    def is_unscoped(self) -> bool:
        return not (self.company_id or self.doc_keys or self.doc_ids or self.fiscal_years)


StructuralTargetKind = Literal["statement", "note", "table", "page", "section", "references"]


class StructuralTarget(BaseModel):
    """One structural lookup, applied to every scoped Report.

    statement: `statement` (+ scope bases) -> the statement's whole table.
    note: `note_ref` and/or `title` (case-insensitive substring) -> the note.
    table / page / section: `index` (table index, 1-based page, section index).
    references: `evidence_id` of a statement or note line -> the notes it
    cites (FOG REFERENCES edges)."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    target: StructuralTargetKind
    statement: StatementName | None = None
    note_ref: Annotated[str, StringConstraints(pattern=r"^[^\s:]{1,64}$")] | None = None
    title: Annotated[str, StringConstraints(min_length=2, max_length=200)] | None = None
    index: int | None = Field(default=None, ge=0, le=1_000_000)
    evidence_id: Annotated[str, StringConstraints(max_length=512)] | None = None

    @model_validator(mode="after")
    def _has_what_it_needs(self) -> StructuralTarget:
        need: dict[str, bool] = {
            "statement": self.statement is not None,
            "note": self.note_ref is not None or self.title is not None,
            "table": self.index is not None,
            "page": self.index is not None and self.index >= 1,
            "section": self.index is not None,
            "references": (
                (ref := parse_evidence_id(self.evidence_id)) is not None
                and ref.kind is EvidenceKind.LINE_ITEM
            ),
        }
        if not need[self.target]:
            raise ValueError(f"structural target {self.target!r} is missing its locator")
        return self


class RetrievalRequest(BaseModel):
    """A retrieval request. `paths` empty means `auto`: hybrid for free
    text, structured for a concept, structural for targets (plus expansion
    of the top table hits), cross_document when a Company or several Fiscal
    years resolve to more than one Report."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    query: Annotated[str, StringConstraints(max_length=4000)] | None = None
    concept: Annotated[str, StringConstraints(pattern=r"^[A-Za-z0-9_.-]{1,64}$")] | None = None
    scope: Scope = Field(default_factory=Scope)
    paths: tuple[RetrievalPath, ...] = ()
    targets: tuple[StructuralTarget, ...] = Field(default=(), max_length=20)
    k: int = Field(default=10, ge=1, le=_MAX_K)
    expand: bool = True
    channels: tuple[Channel, ...] = ("dense", "bm25")

    @model_validator(mode="after")
    def _asks_something(self) -> RetrievalRequest:
        has_query = bool(self.query and self.query.strip())
        if not (has_query or self.concept or self.targets):
            raise ValueError("a request needs a query, a concept or structural targets")
        paths = set(self.paths)
        if RetrievalPath.HYBRID in paths and not has_query:
            raise ValueError("the hybrid path needs a query")
        if RetrievalPath.STRUCTURED in paths and not self.concept:
            raise ValueError("the structured path needs a concept")
        if RetrievalPath.STRUCTURAL in paths and not self.targets:
            raise ValueError("the structural path needs targets")
        if RetrievalPath.CROSS_DOCUMENT in paths and not (has_query or self.concept):
            raise ValueError("the cross_document path needs a query or a concept")
        if not self.channels:
            raise ValueError("at least one hybrid channel is required")
        return self


# ---------------------------------------------------------------------------
# Package
# ---------------------------------------------------------------------------

PathStatus = Literal["ok", "degraded", "skipped"]


@dataclass(frozen=True)
class PathOutcome:
    path: str
    status: PathStatus
    reason: str | None = None
    evidence_count: int = 0


@dataclass(frozen=True)
class EvidencePackage:
    """What `retrieve` returns: the Evidence (structured facts, then
    structural lookups, then hybrid hits, each Evidence once), the scoped
    Reports with their status, which paths ran and how, and -- for a
    cross-document request -- the Evidence grouped by Fiscal year."""

    evidence: tuple[Evidence, ...]
    reports: tuple[ReportOutcome, ...]
    paths: tuple[PathOutcome, ...]
    by_fiscal_year: tuple[FiscalYearGroup, ...] | None = None

    def path(self, name: str) -> PathOutcome | None:
        return next((p for p in self.paths if p.path == name), None)


@dataclass(frozen=True)
class InspectedEvidence:
    """One Evidence with its full context: the whole section, the whole
    table grid with headers, the page, the note -- enough to check a claim
    against the source rather than a snippet."""

    evidence: Evidence
    context: Mapping[str, object] = field(default_factory=dict)


def _meta_body(r: ReportMeta) -> dict[str, object]:
    return {
        "doc_id": r.doc_id, "doc_key": r.doc_key, "version": r.version,
        "company_id": r.company_id, "fiscal_year": r.fiscal_year,
    }


def _outcome_body(o: ReportOutcome) -> dict[str, object]:
    return {**_meta_body(o.report), "status": o.status.value, "evidence_count": o.evidence_count}


def package_body(p: EvidencePackage) -> dict[str, object]:
    return {
        "evidence": [evidence_body(e) for e in p.evidence],
        "reports": [_outcome_body(o) for o in p.reports],
        "paths": [
            {"path": o.path, "status": o.status, "reason": o.reason,
             "evidence_count": o.evidence_count}
            for o in p.paths
        ],
        "by_fiscal_year": None if p.by_fiscal_year is None else [
            {
                "fiscal_year": g.fiscal_year,
                "reports": [_outcome_body(o) for o in g.reports],
                "evidence_ids": [e.evidence_id for e in g.evidence],
            }
            for g in p.by_fiscal_year
        ],
    }


def inspected_body(i: InspectedEvidence) -> dict[str, object]:
    return {"evidence": evidence_body(i.evidence), "context": dict(i.context)}


# ---------------------------------------------------------------------------
# Service
# ---------------------------------------------------------------------------

# Table hits whose whole grid replaces their location card in `auto`
# (structural expansion). Bounded: each costs one table render, and the
# lower-ranked hits are rarely the one an answer rests on.
_EXPAND_TOP = 3

# The stage a path reads: a Report without it is `not_processed` for that path.
_PATH_STAGE = {
    RetrievalPath.HYBRID: "index",
    RetrievalPath.STRUCTURED: "fog_mapping",
    RetrievalPath.STRUCTURAL: "structure",
}


def _report_meta(d: Document) -> ReportMeta:
    return ReportMeta(
        doc_id=d.id, doc_key=d.doc_key, version=d.version,
        company_id=d.company_id, fiscal_year=d.fiscal_year,
    )


class _Artifacts:
    """Per-call artifact reads, each (doc, stage) read at most once. A read
    failure is "no artifact" (`read_artifact` never raises)."""

    def __init__(self, session: AsyncSession, store: ArtifactStore) -> None:
        self._repo = RegistryRepository(session)
        self._store = store
        self._cache: dict[tuple[str, str], Mapping[str, object] | None] = {}

    async def get(self, doc_id: str, stage: str) -> Mapping[str, object] | None:
        key = (doc_id, stage)
        if key not in self._cache:
            self._cache[key] = await read_artifact(self._repo, self._store, doc_id, stage)
        return self._cache[key]

    async def processed(self, doc_id: str, stage: str) -> bool:
        return await self._repo.latest_completed_run(doc_id, stage) is not None


_PathResult = tuple[list[Evidence], str | None]  # (evidence, skipped reason)


class RetrievalService:
    """The one retrieval surface (S4). Build one per request/unit of work:
    it shares the caller's session and caches artifact reads for its life."""

    def __init__(
        self,
        *,
        session: AsyncSession,
        store: ArtifactStore,
        embedder: EmbeddingClient | None = None,
        vector_store: VectorStore | None = None,
    ) -> None:
        self._session = session
        self._store = store
        self._embedder = embedder
        self._vector_store = vector_store
        self._artifacts = _Artifacts(session, store)

    # -- retrieve ---------------------------------------------------------

    async def retrieve(self, request: RetrievalRequest) -> EvidencePackage:
        try:
            reports = await self._resolve(request.scope)
        except SQLAlchemyError as exc:
            await self._rollback()
            log.warning("retrieval.scope_failed", error=repr(exc))
            reason = f"scope could not be resolved: {type(exc).__name__}"
            return EvidencePackage(
                evidence=(), reports=(),
                paths=tuple(PathOutcome(p.value, "degraded", reason) for p in RetrievalPath),
            )

        paths = self._choose_paths(request, reports)
        cross = RetrievalPath.CROSS_DOCUMENT in paths
        outcomes: list[PathOutcome] = []
        found: dict[RetrievalPath, list[Evidence]] = {}

        async def run(path: RetrievalPath, fn: Callable[[], Awaitable[_PathResult]]) -> None:
            evidence, outcome = await self._guarded(path.value, fn)
            found[path] = evidence
            outcomes.append(outcome)

        if RetrievalPath.STRUCTURED in paths:
            await run(RetrievalPath.STRUCTURED,
                      lambda: self._structured(request, reports))
        if RetrievalPath.STRUCTURAL in paths:
            await run(RetrievalPath.STRUCTURAL,
                      lambda: self._structural(request, reports))
        if RetrievalPath.HYBRID in paths:
            await run(RetrievalPath.HYBRID,
                      lambda: self._hybrid(request, reports, per_report=cross))

        expand = (
            not request.paths and request.expand and RetrievalPath.HYBRID in found
        )
        if expand and found[RetrievalPath.HYBRID]:
            expanded, outcome = await self._guarded(
                "structural", lambda: self._expand(found[RetrievalPath.HYBRID])
            )
            if expanded:
                found[RetrievalPath.HYBRID] = expanded
            if RetrievalPath.STRUCTURAL not in paths:
                outcomes.append(replace(outcome, reason=outcome.reason or "expansion"))

        evidence = merge_evidence(
            found.get(RetrievalPath.STRUCTURED, []),
            found.get(RetrievalPath.STRUCTURAL, []),
            found.get(RetrievalPath.HYBRID, []),
        )
        report_outcomes = await self._report_outcomes(reports, paths, evidence)
        if cross:
            outcomes.append(PathOutcome(
                RetrievalPath.CROSS_DOCUMENT.value, "ok",
                evidence_count=len(evidence),
            ))
        return EvidencePackage(
            evidence=tuple(evidence),
            reports=tuple(report_outcomes),
            paths=tuple(outcomes),
            by_fiscal_year=(
                tuple(group_by_fiscal_year(report_outcomes, evidence)) if cross else None
            ),
        )

    def _choose_paths(
        self, request: RetrievalRequest, reports: list[ReportMeta] | None
    ) -> list[RetrievalPath]:
        if request.paths:
            chosen = list(dict.fromkeys(request.paths))
            if RetrievalPath.CROSS_DOCUMENT in chosen:
                # Cross-document fans the content paths out per Report; it is
                # not a store of its own.
                if request.query and request.query.strip() and RetrievalPath.HYBRID not in chosen:
                    chosen.append(RetrievalPath.HYBRID)
                if request.concept and RetrievalPath.STRUCTURED not in chosen:
                    chosen.append(RetrievalPath.STRUCTURED)
            return chosen
        chosen = []
        if request.concept:
            chosen.append(RetrievalPath.STRUCTURED)
        if request.targets:
            chosen.append(RetrievalPath.STRUCTURAL)
        if request.query and request.query.strip():
            chosen.append(RetrievalPath.HYBRID)
        multi_scope = bool(request.scope.company_id) or len(request.scope.fiscal_years) > 1
        if multi_scope and reports is not None and len(reports) > 1 and (
            request.concept or (request.query and request.query.strip())
        ):
            chosen.append(RetrievalPath.CROSS_DOCUMENT)
        return chosen

    async def _guarded(
        self, path: str, fn: Callable[[], Awaitable[_PathResult]]
    ) -> tuple[list[Evidence], PathOutcome]:
        try:
            evidence, skipped = await fn()
        except Exception as exc:  # noqa: BLE001 - a path degrades; retrieve never raises
            if isinstance(exc, SQLAlchemyError):
                await self._rollback()
            log.warning("retrieval.path_degraded", path=path, error=repr(exc))
            return [], PathOutcome(path, "degraded", f"{type(exc).__name__}: {exc}")
        if skipped is not None:
            return [], PathOutcome(path, "skipped", skipped)
        return evidence, PathOutcome(path, "ok", evidence_count=len(evidence))

    async def _rollback(self) -> None:
        # A failed statement poisons the shared session for every later read;
        # a store error leaves it clean. Never let the rollback itself raise.
        with contextlib.suppress(Exception):
            await self._session.rollback()

    # -- scope --------------------------------------------------------------

    async def _resolve(self, scope: Scope) -> list[ReportMeta] | None:
        """Scope -> the Report versions it covers, in the registry's order
        (newest Fiscal year first). None means "every version of every
        Report, unfiltered" -- only an unscoped `all_versions` request, the
        pre-S4 routes' own semantics."""
        docs = DocumentRepository(self._session)
        if scope.is_unscoped:
            if scope.all_versions:
                return None
            return [_report_meta(d) for d in await docs.latest_versions()]

        candidates: list[Document] | None = None

        def narrow(rows: Sequence[Document]) -> None:
            nonlocal candidates
            if candidates is None:
                candidates = list(rows)
            else:
                keep = {d.id for d in rows}
                candidates = [d for d in candidates if d.id in keep]

        if scope.company_id:
            narrow(await CompanyRegistry(self._session, self._store)
                   .report_documents(scope.company_id))
        if scope.doc_keys:
            if scope.version is not None:
                ids = await docs.ids_for(doc_key=scope.doc_keys[0], version=scope.version)
                narrow(list((await docs.get_many(ids)).values()))
            elif scope.all_versions:
                ids = [
                    i for k in scope.doc_keys
                    for i in await docs.ids_for(doc_key=k, version=None)
                ]
                narrow(list((await docs.get_many(ids)).values()))
            else:
                narrow(await docs.latest_versions(doc_keys=scope.doc_keys))
        if scope.doc_ids:
            by_id = await docs.get_many(list(scope.doc_ids))
            narrow([by_id[i] for i in dict.fromkeys(scope.doc_ids) if i in by_id])
        if scope.fiscal_years:
            years = set(scope.fiscal_years)
            if candidates is None:
                narrow(await docs.latest_versions(fiscal_years=scope.fiscal_years))
            else:
                narrow([d for d in candidates if d.fiscal_year in years])
        return [_report_meta(d) for d in candidates or []]

    async def _report_outcomes(
        self,
        reports: list[ReportMeta] | None,
        paths: Sequence[RetrievalPath],
        evidence: Sequence[Evidence],
    ) -> list[ReportOutcome]:
        if reports is None:
            return []
        stages = [s for p, s in _PATH_STAGE.items() if p in paths]
        counts: dict[str, int] = {}
        for e in evidence:
            counts[e.report.doc_id] = counts.get(e.report.doc_id, 0) + 1
        out: list[ReportOutcome] = []
        for r in reports:
            try:
                processed = any([await self._artifacts.processed(r.doc_id, s) for s in stages])
            except SQLAlchemyError:
                await self._rollback()
                processed = counts.get(r.doc_id, 0) > 0
            n = counts.get(r.doc_id, 0)
            out.append(ReportOutcome(r, report_status(processed=processed, evidence_count=n), n))
        return out

    # -- hybrid -------------------------------------------------------------

    async def _hybrid(
        self, request: RetrievalRequest, reports: list[ReportMeta] | None, *, per_report: bool
    ) -> _PathResult:
        if self._embedder is None or self._vector_store is None:
            raise RuntimeError("vector store not configured")
        scope = request.scope
        indexable = {EvidenceKind.PASSAGE, EvidenceKind.TABLE}
        kinds = [k.value for k in scope.kinds if k in indexable]
        if scope.kinds and not kinds:
            return [], "requested Evidence kinds are not in the vector index"
        if reports is not None and not reports:
            return [], "scope matched no Reports"
        query = (request.query or "").strip()
        base: dict[str, object] = {
            "bases": [b.value for b in scope.bases],
            "kinds": kinds,
            "section_types": [t.value for t in scope.section_types],
        }
        groups = [[r] for r in reports] if per_report and reports else [reports]
        evidence: list[Evidence] = []
        for group in groups:
            filters = dict(base)
            if group is not None:
                filters["doc_ids"] = [r.doc_id for r in group]
            hits = await self._channels(request, query, filters)
            evidence.extend(await self._hits_to_evidence(hits, group))
        return evidence, None

    async def _channels(
        self, request: RetrievalRequest, query: str, filters: Mapping[str, object]
    ) -> list[dict[str, object]]:
        assert self._embedder is not None and self._vector_store is not None
        channels = set(request.channels)
        if channels == {"dense", "bm25"}:
            return await hybrid_search(
                session=self._session, embedder=self._embedder,
                vector_store=self._vector_store, query=query, k=request.k, filters=filters,
            )
        if channels == {"dense"}:
            return await vector_search(
                session=self._session, embedder=self._embedder,
                vector_store=self._vector_store, query=query, k=request.k, filters=filters,
            )
        return await bm25_search(
            session=self._session, vector_store=self._vector_store,
            query=query, k=request.k, filters=filters,
        )

    async def _hits_to_evidence(
        self, hits: Sequence[Mapping[str, object]], group: list[ReportMeta] | None
    ) -> list[Evidence]:
        metas = {r.doc_id: r for r in group} if group is not None else {}
        missing = [str(h.get("doc_id")) for h in hits if h.get("doc_id") not in metas]
        if missing:
            rows = await DocumentRepository(self._session).get_many(sorted(set(missing)))
            metas.update({i: _report_meta(d) for i, d in rows.items()})
        out: list[Evidence] = []
        for h in hits:
            meta = metas.get(str(h.get("doc_id")))
            if meta is None:  # a row for a deleted Document: never cite it
                continue
            e = evidence_from_hit(h, meta, path=RetrievalPath.HYBRID.value)
            if e is not None:
                out.append(e)
        return out

    async def _expand(self, hits: list[Evidence]) -> _PathResult:
        """Structural expansion: the top table hits' location cards are
        replaced by their whole grids, linked to the notes that hold them."""
        out: list[Evidence] = []
        expanded = 0
        for e in hits:
            ref = parse_evidence_id(e.evidence_id)
            if expanded >= _EXPAND_TOP or ref is None or ref.kind is not EvidenceKind.TABLE:
                out.append(e)
                continue
            structure = await self._artifacts.get(ref.doc_id, "structure") or {}
            table = ctxv.table_at(structure, cast(int, ref.index))
            if table is None:
                out.append(e)
                continue
            identity = e.content.split("\n", 1)[0].strip().strip("[]")
            fog = await self._artifacts.get(ref.doc_id, "fog_objects") or {}
            links = tuple(
                r.id for _, nid in ctxv.notes_for_table(fog, cast(int, ref.index))
                if (r := ref_for_fog_node(ref.doc_id, nid)) is not None
            )
            out.append(replace(
                e,
                content=ctxv.table_markdown(table, identity=identity) or e.content,
                links=links,
                bounding_regions=ctxv.table_regions(table),
                retrieval_paths=e.retrieval_paths + (RetrievalPath.STRUCTURAL.value,),
            ))
            expanded += 1
        return out, None

    # -- structured ---------------------------------------------------------

    async def _structured(
        self, request: RetrievalRequest, reports: list[ReportMeta] | None
    ) -> _PathResult:
        if not reports:
            return [], "structured lookup needs a Company, Report or Fiscal-year scope"
        concept = cast(str, request.concept)
        wanted = [b.value for b in request.scope.bases if b is not Basis.UNKNOWN]
        if request.scope.bases and not wanted:
            return [], "facts exist only for a statement basis; the scope names none"
        out: list[Evidence] = []
        for r in reports:
            fog = await self._artifacts.get(r.doc_id, "fog_objects")
            mapping = await self._artifacts.get(r.doc_id, "fog_mapping")
            if fog is None or mapping is None:
                continue
            for basis in wanted or ctxv.fog_bases(fog):
                claim = fact_claims(fog, mapping, basis=basis).get(concept)
                if claim is None:
                    continue
                item = ctxv.line_item(fog, basis, claim.line_item) or {}
                li = ref_for_fog_node(r.doc_id, claim.line_item)
                out.append(Evidence(
                    evidence_id=fact_ref(r.doc_id, basis, concept).id,
                    kind=EvidenceKind.FACT,
                    report=r,
                    content=f"{claim.label}: {claim.value:,.2f} ({concept}, {basis})",
                    page=cast(int | None, item.get("page")),
                    section=cast(str | None, item.get("caption")) or None,
                    basis=basis,
                    table_index=cast(int | None, item.get("table_index")),
                    links=(li.id,) if li is not None else (),
                    value=claim.value,
                    retrieval_paths=(RetrievalPath.STRUCTURED.value,),
                ))
        return out, None

    # -- structural ---------------------------------------------------------

    async def _structural(
        self, request: RetrievalRequest, reports: list[ReportMeta] | None
    ) -> _PathResult:
        out: list[Evidence] = []
        refs_targets = [t for t in request.targets if t.target == "references"]
        doc_targets = [t for t in request.targets if t.target != "references"]
        for t in refs_targets:
            out.extend(await self._follow_references(cast(str, t.evidence_id)))
        if doc_targets:
            if not reports:
                if not out:
                    return [], "structural lookup needs a Company, Report or Fiscal-year scope"
            else:
                for r in reports:
                    for t in doc_targets:
                        out.extend(await self._target(r, t, request.scope))
        return out, None

    async def _target(self, r: ReportMeta, t: StructuralTarget, scope: Scope) -> list[Evidence]:
        bases = [b.value for b in scope.bases if b is not Basis.UNKNOWN]
        if t.target in ("statement", "note") and scope.bases and not bases:
            return []  # statements and notes always carry a statement basis
        if t.target == "statement":
            fog = await self._artifacts.get(r.doc_id, "fog_objects") or {}
            out = []
            for basis in bases or ctxv.fog_bases(fog):
                for st in ctxv.statements(fog, basis):
                    index = st.get("table_index")
                    if st.get("statement") == t.statement and isinstance(index, int):
                        e = await self._table_evidence(r, index)
                        if e is not None:
                            out.append(replace(e, basis=basis, section=f"{t.statement} ({basis})"))
            return out
        if t.target == "note":
            fog = await self._artifacts.get(r.doc_id, "fog_objects") or {}
            out = []
            for basis in bases or ctxv.fog_bases(fog):
                for note in ctxv.find_notes(fog, basis, ref=t.note_ref, title=t.title):
                    e = await self._note_evidence(r, basis, note)
                    if e is not None:
                        out.append(e)
            return out
        index = cast(int, t.index)
        e = None
        if t.target == "table":
            e = await self._table_evidence(r, index)
        elif t.target == "page":
            e = await self._page_evidence(r, index)
        elif t.target == "section":
            e = await self._passage_evidence(r, index)
        if e is None or (scope.bases and e.basis not in {b.value for b in scope.bases}):
            return []
        return [e]

    async def _follow_references(self, line_item_id: str) -> list[Evidence]:
        ref = parse_evidence_id(line_item_id)
        if ref is None or ref.kind is not EvidenceKind.LINE_ITEM:
            return []
        doc = await DocumentRepository(self._session).get_many([ref.doc_id])
        if ref.doc_id not in doc:
            return []
        r = _report_meta(doc[ref.doc_id])
        fog = await self._artifacts.get(r.doc_id, "fog_objects") or {}
        basis = cast(str, ref.basis)
        out = []
        for target in ctxv.references_from(fog, basis, cast(str, ref.fog_id)):
            note = ctxv.note_by_id(fog, basis, target)
            if note is not None and (e := await self._note_evidence(r, basis, note)) is not None:
                out.append(replace(e, links=(line_item_id, *e.links)))
        return out

    # -- Evidence builders (shared by retrieve and inspect) ------------------

    async def _basis_of_page(self, doc_id: str, page: int | None) -> str:
        basis_art = await self._artifacts.get(doc_id, "basis") or {}
        return page_basis(basis_art, page)

    async def _table_evidence(self, r: ReportMeta, index: int) -> Evidence | None:
        structure = await self._artifacts.get(r.doc_id, "structure") or {}
        table = ctxv.table_at(structure, index)
        if table is None:
            return None
        page = ctxv.table_page(table)
        caption = ctxv.table_caption(table, index)
        fog = await self._artifacts.get(r.doc_id, "fog_objects") or {}
        links = tuple(
            ref.id for _, nid in ctxv.notes_for_table(fog, index)
            if (ref := ref_for_fog_node(r.doc_id, nid)) is not None
        )
        return Evidence(
            evidence_id=table_ref(r.doc_id, index).id, kind=EvidenceKind.TABLE, report=r,
            content=ctxv.table_markdown(table, identity=caption), page=page, section=caption,
            basis=await self._basis_of_page(r.doc_id, page), table_index=index, links=links,
            bounding_regions=ctxv.table_regions(table),
            retrieval_paths=(RetrievalPath.STRUCTURAL.value,),
        )

    async def _note_evidence(
        self, r: ReportMeta, basis: str, note: Mapping[str, object]
    ) -> Evidence | None:
        ref = ref_for_fog_node(r.doc_id, note.get("id"))
        if ref is None:
            return None
        ocr = await self._artifacts.get(r.doc_id, "ocr") or {}
        title = str(note.get("title", ""))
        body = ctxv.window_text(ocr, note.get("start_offset"), note.get("end_offset"))
        tables = note.get("table_indices")
        return Evidence(
            evidence_id=ref.id, kind=EvidenceKind.NOTE, report=r,
            content=ctxv.clip(body or title), page=cast(int | None, note.get("page")),
            section=title or None, basis=basis,
            links=tuple(
                table_ref(r.doc_id, i).id for i in (tables if isinstance(tables, list) else [])
                if isinstance(i, int)
            ),
            retrieval_paths=(RetrievalPath.STRUCTURAL.value,),
        )

    async def _page_evidence(self, r: ReportMeta, page: int) -> Evidence | None:
        ocr = await self._artifacts.get(r.doc_id, "ocr") or {}
        text = ctxv.page_text(ocr, page)
        if text is None:
            return None
        return Evidence(
            evidence_id=page_ref(r.doc_id, page).id, kind=EvidenceKind.PAGE, report=r,
            content=ctxv.clip(text), page=page,
            basis=await self._basis_of_page(r.doc_id, page),
            retrieval_paths=(RetrievalPath.STRUCTURAL.value,),
        )

    async def _passage_evidence(self, r: ReportMeta, index: int) -> Evidence | None:
        chunks = await self._artifacts.get(r.doc_id, "chunks") or {}
        record = ctxv.chunk_record(chunks, f"s{index}")
        if record is None:
            return None
        page = record.get("page") if isinstance(record.get("page"), int) else None
        return Evidence(
            evidence_id=passage_ref(r.doc_id, index).id, kind=EvidenceKind.PASSAGE, report=r,
            content=str(record.get("text", "")), page=cast(int | None, page),
            section=cast(str | None, record.get("heading")),
            section_type=cast(str | None, record.get("section_type")),
            basis=await self._basis_of_page(r.doc_id, cast(int | None, page)),
            retrieval_paths=(RetrievalPath.STRUCTURAL.value,),
        )

    # -- inspect ------------------------------------------------------------

    async def inspect(self, evidence_id: str) -> InspectedEvidence | None:
        """An Evidence id -> that Evidence with its full context, or None when
        the id is malformed or names nothing that exists (an unknown Report,
        a table index past the end, a fact the Report does not state)."""
        ref = parse_evidence_id(evidence_id)
        if ref is None:
            return None
        rows = await DocumentRepository(self._session).get_many([ref.doc_id])
        if ref.doc_id not in rows:
            return None
        r = _report_meta(rows[ref.doc_id])
        match ref.kind:
            case EvidenceKind.PASSAGE:
                return await self._inspect_passage(r, ref)
            case EvidenceKind.TABLE | EvidenceKind.TABLE_CELL:
                return await self._inspect_table(r, ref)
            case EvidenceKind.PAGE:
                e = await self._page_evidence(r, cast(int, ref.index))
                return None if e is None else InspectedEvidence(
                    replace(e, retrieval_paths=()), {"page_text": e.content}
                )
            case EvidenceKind.NOTE:
                return await self._inspect_note(r, ref)
            case EvidenceKind.LINE_ITEM:
                return await self._inspect_line_item(r, ref)
            case EvidenceKind.FACT:
                return await self._inspect_fact(r, ref)

    async def _inspect_passage(self, r: ReportMeta, ref: EvidenceRef) -> InspectedEvidence | None:
        e = await self._passage_evidence(r, cast(int, ref.index))
        if e is None:
            return None
        ocr = await self._artifacts.get(r.doc_id, "ocr") or {}
        page_text = ctxv.page_text(ocr, e.page) if e.page is not None else None
        regions = await self._passage_regions(r.doc_id, f"s{ref.index}")
        return InspectedEvidence(
            replace(e, retrieval_paths=(), bounding_regions=regions),
            {"section_text": e.content, "page_text": ctxv.clip(page_text or "")},
        )

    async def _passage_regions(self, doc_id: str, node_id: str) -> tuple[Mapping[str, object], ...]:
        prov = await self._artifacts.get(doc_id, "provenance") or {}
        records = prov.get("node_provenance")
        for rec in records if isinstance(records, list) else []:
            if isinstance(rec, Mapping) and rec.get("node_id") == node_id:
                regions = rec.get("bounding_regions")
                if isinstance(regions, list):
                    return tuple(x for x in regions if isinstance(x, Mapping))
                return ()
        return ()

    async def _inspect_table(self, r: ReportMeta, ref: EvidenceRef) -> InspectedEvidence | None:
        table_index = cast(int, ref.index)
        e = await self._table_evidence(r, table_index)
        if e is None:
            return None
        structure = await self._artifacts.get(r.doc_id, "structure") or {}
        table = cast(Mapping[str, object], ctxv.table_at(structure, table_index))
        context: dict[str, object] = {"grid": ctxv.grid_body(table), "markdown": e.content}
        if ref.kind is EvidenceKind.TABLE_CELL:
            c = ctxv.cell(table, cast(int, ref.row), cast(int, ref.col))
            if c is None:
                return None
            context["cell"] = c
            e = replace(
                e, evidence_id=cell_ref(r.doc_id, table_index, cast(int, ref.row),
                                        cast(int, ref.col)).id,
                kind=EvidenceKind.TABLE_CELL,
                content=f"{c['row_label']} | {c['column_header']}: {c['value']}".strip(" |:"),
                cell_refs=(ref.id,), links=(table_ref(r.doc_id, table_index).id, *e.links),
            )
        return InspectedEvidence(replace(e, retrieval_paths=()), context)

    async def _inspect_note(self, r: ReportMeta, ref: EvidenceRef) -> InspectedEvidence | None:
        fog = await self._artifacts.get(r.doc_id, "fog_objects") or {}
        basis = cast(str, ref.basis)
        note = ctxv.note_by_id(fog, basis, cast(str, ref.fog_id))
        if note is None:
            return None
        e = await self._note_evidence(r, basis, note)
        if e is None:
            return None
        tables = []
        for tid in e.links:
            t = parse_evidence_id(tid)
            if t is not None and (te := await self._table_evidence(r, cast(int, t.index))):
                tables.append({"evidence_id": te.evidence_id, "markdown": te.content})
        return InspectedEvidence(
            replace(e, retrieval_paths=()),
            {"title": note.get("title"), "ref": note.get("ref"), "text": e.content,
             "tables": tables},
        )

    async def _inspect_line_item(
        self, r: ReportMeta, ref: EvidenceRef
    ) -> InspectedEvidence | None:
        fog = await self._artifacts.get(r.doc_id, "fog_objects") or {}
        basis = cast(str, ref.basis)
        fog_id = cast(str, ref.fog_id)
        item = ctxv.line_item(fog, basis, fog_id)
        if item is None:
            return None
        parent = ctxv.parent_node(fog, basis, str(item.get("parent", ""))) or {}
        cites = [
            n.id for t in ctxv.references_from(fog, basis, fog_id)
            if (n := ref_for_fog_node(r.doc_id, t)) is not None
        ]
        table_index = item.get("table_index")
        table_md = ""
        table_id = None
        if isinstance(table_index, int) and table_index >= 0:
            te = await self._table_evidence(r, table_index)
            if te is not None:
                table_md, table_id = te.content, te.evidence_id
        label = str(item.get("label", ""))
        e = Evidence(
            evidence_id=ref.id, kind=EvidenceKind.LINE_ITEM, report=r,
            content=f"{label}: current {item.get('current')}, prior {item.get('prior')}",
            page=cast(int | None, item.get("page")),
            section=cast(str | None, item.get("caption")) or None, basis=basis,
            table_index=table_index if isinstance(table_index, int) else None,
            links=tuple(x for x in (table_id, *cites) if x),
        )
        return InspectedEvidence(e, {
            "line_item": dict(item), "parent": dict(parent),
            "references": cites, "table": table_md,
        })

    async def _inspect_fact(self, r: ReportMeta, ref: EvidenceRef) -> InspectedEvidence | None:
        fog = await self._artifacts.get(r.doc_id, "fog_objects")
        mapping = await self._artifacts.get(r.doc_id, "fog_mapping")
        if fog is None or mapping is None:
            return None
        basis, tag = cast(str, ref.basis), cast(str, ref.name)
        claim = fact_claims(fog, mapping, basis=basis).get(tag)
        if claim is None:
            return None
        li = ref_for_fog_node(r.doc_id, claim.line_item)
        line = await self._inspect_line_item(r, li) if li is not None else None
        item = line.evidence if line is not None else None
        e = Evidence(
            evidence_id=ref.id, kind=EvidenceKind.FACT, report=r,
            content=f"{claim.label}: {claim.value:,.2f} ({tag}, {basis})",
            page=item.page if item else None, section=item.section if item else None,
            basis=basis, table_index=item.table_index if item else None,
            links=(li.id,) if li is not None else (), value=claim.value,
        )
        return InspectedEvidence(e, {
            "fact": {"tag": tag, "basis": basis, "value": claim.value, "label": claim.label,
                     "origin": claim.origin, "method": claim.method},
            "line_item": li.id if li is not None else None,
            "table": line.context.get("table") if line is not None else "",
        })


# ---------------------------------------------------------------------------
# Compatibility: the pre-S4 search surfaces, served by the service
# ---------------------------------------------------------------------------


class RetrievalUnavailable(RuntimeError):
    """A pre-S4 search surface asked for the hybrid path and it degraded.
    Those surfaces always raised on a store failure (their routes answer 500,
    the `/ask` tool reports an error); they keep doing so rather than
    returning a confident empty list."""


_SECTION_TYPE_VALUES = frozenset(t.value for t in SectionType)


async def legacy_search(
    service: RetrievalService,
    *,
    session: AsyncSession,
    query: str,
    k: int,
    section_type: str | None = None,
    doc_key: str | None = None,
    version: int | None = None,
    channels: tuple[Channel, ...] = ("dense", "bm25"),
) -> list[dict[str, object]]:
    """`POST /retrieve`, `/retrieve/vector`, `/retrieve/keyword` and the
    `/ask` `search` tool, through the service's hybrid path with their
    original semantics: every version of a `doc_key` unless `version` names
    one, no filter at all when none is given, an unknown section type or
    doc_key matches nothing. Returns their original citation-shaped hits,
    plus `evidence_id`/`basis`/`kind`."""
    if not query.strip() or k < 1:
        return []
    k = min(k, _MAX_K)
    if section_type is not None and section_type not in _SECTION_TYPE_VALUES:
        return []
    section_types = (SectionType(section_type),) if section_type is not None else ()
    try:
        if doc_key is not None:
            scope = Scope(doc_keys=(doc_key,), version=version, all_versions=True,
                          section_types=section_types)
        elif version is not None:
            ids = await DocumentRepository(session).ids_for(doc_key=None, version=version)
            if not ids:
                return []
            scope = Scope(doc_ids=tuple(ids[:1000]), all_versions=True,
                          section_types=section_types)
        else:
            scope = Scope(all_versions=True, section_types=section_types)
        request = RetrievalRequest(
            query=query, scope=scope, paths=(RetrievalPath.HYBRID,), k=k, channels=channels,
        )
    except ValidationError:
        # A filter value no Report can carry (an over-long doc_key, a
        # version below 1) matched nothing before S4; it still does.
        return []
    package = await service.retrieve(request)
    outcome = package.path(RetrievalPath.HYBRID.value)
    if outcome is not None and outcome.status == "degraded":
        raise RetrievalUnavailable(outcome.reason or "hybrid retrieval degraded")
    return [legacy_hit(e) for e in package.evidence]
