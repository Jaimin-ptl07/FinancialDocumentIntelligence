"""The extractor's orchestrator + ExtractorClient seam (design spec sec
3.2): extract_unit pools context ONCE per Retrieval Unit via hybrid_search
ALONE (dense BGE + keyword Postgres-FTS, RRF-fused -- table content already
reaches this pool as chunks, since build_chunks emits one table CARD
(`t{i}`) per canonicalized table alongside `s{i}` section chunks and
EmbeddingStage embeds all of them; a separate get_tables table-blob
injection was measured to flood the rerank with same-scored whole-table
renders and HURT top-20 recall, 78.8% -> 88.1% once removed -- see
docs/architecture/2026-07-19-ingestion-audit-and-improvement-plan.md),
scopes the pool with fdi.core.extractor's `filter_candidates` (drops
whole-table chunks for units with no numeric/list field) plus TR-1 P4b's
own `_filter_by_basis` (drops candidates whose basis is the OPPOSITE of the
unit's `basis_preference` -- replacing the old single-cutoff "drop
candidates at/after the Consolidated-section boundary" filter with a
labeled-region model that also handles reverse-ordered and standalone-only
filings; see docs/superpowers/specs/2026-07-22-table-representation-
design.md sec 6.2), reranks with an adaptive top_k (unit_top_k -- bigger
units get a bigger pool so no field is starved), renders each ranked TABLE
CARD's grid to markdown (TR-1 P3-wire's `_render_table_candidate` -- a
section-chunk candidate is untouched), prompts the client ONCE with every
field in the unit batched into a single call (schema v3.1's "Retrieve-once,
extract-many"), and assembles one output_contract record per field.
extract_document groups fields into Retrieval Units (group_fields_by_unit)
and runs one retrieval + one LLM call per unit, then restores the caller's
field order -- NOT one call per field, the pre-batching behaviour.
ExtractorClient.extract(*, system, user) -> Mapping is SYNCHRONOUS,
mirroring fdi.pipeline.agent.AgentClient.complete exactly.
AzureOpenAIExtractorClient (fdi.platform.extractor, does I/O) structurally
satisfies this protocol without importing it -- platform may not import
pipeline. FakeExtractorClient lives here for tests: no live Azure OpenAI
call anywhere in the default suite. extract_field (single-field pooling/
prompting) is preserved unchanged for callers that hold one FieldSpec; it is
NOT routed through extract_unit, and is neither basis-filtered nor
table-rendered.

Never raises (decision 10): extract_unit guards every I/O step (hybrid_
search, client.extract) individually, so one UNIT's failure degrades that
unit's fields to well-formed null records, never the whole document's batch
-- extract_field does the same at field granularity for its own callers.
The basis/render helpers below are pure guarded-total functions (never
raise on any input shape), matching fdi.core's own posture even though they
live in this pipeline module (they read the two artifacts extract_document
already fetched, not fresh I/O of their own).

ExtractContext carries doc_id AND doc_key AND version (resolved ONCE by the
caller -- the /extract/document route, which already looks the document up
by doc_key to 404 on an unknown one) rather than just doc_id. hybrid_search's
`filters` are doc_key/version-keyed, not doc_id-keyed; carrying all three on
the context lets extract_field scope hybrid_search to THIS document/version
directly, with no per-field `DocumentRepository.get` lookup inside the loop
-- avoiding an O(fields) redundant single-row lookup per document that
resolving only doc_id would require."""

import contextlib
import hashlib
import json
import re
import uuid
from collections.abc import Collection, Mapping, Sequence
from dataclasses import dataclass, replace
from typing import Protocol, cast, runtime_checkable

import structlog
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession

from fdi.core.basis import page_basis, resolve_effective_basis
from fdi.core.extraction_plan import (
    EngineVersions,
    PlanItem,
    PlannedField,
    StoredRead,
    cache_key,
    plan_extraction,
    summarize,
)
from fdi.core.extractor import (
    EXTRACTION_RECORD_VERSION,
    Candidate,
    ExtractResult,
    FieldSpec,
    UnitSpec,
    assemble_result,
    assemble_unit_results,
    build_extraction_messages,
    build_extraction_record,
    build_unit_messages,
    filter_candidates,
    group_fields_by_unit,
    prompt_version,
    rerank_candidates,
    unit_needs_tables,
    unit_top_k,
)
from fdi.core.notes import (
    NoteMap,
    basis_bounds,
    build_note_map,
    find_statement_pages,
)
from fdi.core.pricing import LlmCallUsage
from fdi.core.provenance import resolve_evidence_regions, resolve_field_regions
from fdi.core.statements import STATEMENT_ANCHORS, find_statement_tables
from fdi.core.table_render import render_table_markdown, repair_headers
from fdi.pipeline.embeddings import EmbeddingClient
from fdi.pipeline.keys import extraction_key, output_hash
from fdi.pipeline.retrieval import hybrid_search, read_chunks
from fdi.pipeline.vectorstore import VectorStore
from fdi.platform.db.extraction_repository import ExtractionRepository
from fdi.platform.db.models import FieldRead
from fdi.platform.db.repository import RegistryRepository
from fdi.platform.storage.artifact_store import ArtifactStore

log = structlog.get_logger(__name__)


@runtime_checkable
class ExtractorClient(Protocol):
    def extract(self, *, system: str, user: str) -> ExtractResult: ...


class FakeExtractorClient:
    """Replays scripted {value, source, confidence}-shaped dicts, one per
    call, in order -- mirrors fdi.pipeline.agent.FakeAgentClient exactly
    (same AssertionError-on-overcall posture, a loud test-diagnosable
    failure rather than a confusing IndexError deep inside extract_field).
    Constructor is unchanged (still `list[Mapping[str, object]]`) -- each
    scripted mapping is wrapped into an ExtractResult with usage=None
    internally, so no existing FakeExtractorClient(...) construction site
    needs updating for the costing feature."""

    def __init__(self, results: list[Mapping[str, object]]) -> None:
        self._results = results
        self._next = 0
        self.calls: list[tuple[str, str]] = []

    model_id = "fake"

    def extract(self, *, system: str, user: str) -> ExtractResult:
        self.calls.append((system, user))
        if self._next >= len(self._results):
            raise AssertionError(
                f"FakeExtractorClient exhausted: {len(self._results)} scripted "
                f"result(s), call {len(self.calls)} requested"
            )
        result = self._results[self._next]
        self._next += 1
        return ExtractResult(data=result)


@dataclass
class ExtractContext:
    session: AsyncSession
    store: ArtifactStore
    embedder: EmbeddingClient | None
    vector_store: VectorStore | None
    doc_id: str
    doc_key: str
    version: int
    # Costing feature (docs/superpowers/specs/2026-07-28-costing-feature-
    # design.md): threaded through to _record_llm_usage's llm_usage_event
    # rows. Optional, appended last -- every existing keyword-only
    # ExtractContext(...) construction site is unaffected.
    correlation_id: str | None = None
    # The API job these calls are spent for, when there is one: what
    # GET /jobs/{job_id}/usage reads by, since correlation_id is caller-
    # supplied and cannot prove whose job a usage row belongs to.
    api_job_id: str | None = None


async def _record_llm_usage(
    *, ctx: ExtractContext, source: str, usage: LlmCallUsage
) -> None:
    """Persist one real ExtractorClient.extract() call's usage. stage_run_id
    is always None here -- extract_document runs outside run_stage entirely
    (it is a synchronous route, not a Celery-dispatched stage)."""
    await RegistryRepository(ctx.session).record_llm_usage(
        str(uuid.uuid4()), doc_id=ctx.doc_id, stage_run_id=None,
        source=source, correlation_id=ctx.correlation_id, api_job_id=ctx.api_job_id,
        model=usage.model, input_tokens=usage.input_tokens,
        output_tokens=usage.output_tokens, duration_ms=usage.duration_ms,
    )


_POOL_K = 60
_EXTRACT_TOP_K = 20

# The pool is over-fetched RELATIVE to top_k, not fixed. The filters run
# BETWEEN pooling and reranking, so a fixed 60 against an adaptive top_k of up
# to 60 would leave large units (RU_BS) with fewer post-filter candidates than
# top_k -- strictly less context than the harness supplied, a silent recall
# regression. top_k*2 mirrors the harness's fuse(k=top_k*2).
_POOL_OVERFETCH = 2

# Note candidates are FETCHED from a printed reference, not guessed by
# similarity, so they outrank every searched candidate and survive reranking.
_NOTE_SCORE = 1_000.0

# Cap injected note chunks so a long schedule cannot crowd out the search pool.
_NOTE_MAX = 12


def _note_candidates(
    chunks: Sequence[Mapping[str, object]], pages: Sequence[int], doc_id: str
) -> list[Candidate]:
    """Chunks lying on a resolved note's pages, as Candidates.

    This is the deterministic half of retrieval: the note reference printed on
    the face row (`Property, plant and equipment | 8(a)`) already says where the
    breakdown lives, so those pages are FETCHED rather than searched for.
    Similarity cannot do this -- "Land" is too generic and the schedule sits ~30
    pages from the face -- which is why ~50 taxonomy leaves came back null on
    TCS. Scored above the search pool so reranking cannot drop them."""
    wanted = set(pages)
    if not wanted:
        return []
    out: list[Candidate] = []
    for chunk in chunks:
        if not isinstance(chunk, Mapping):
            continue
        page = chunk.get("page")
        if not isinstance(page, int) or page not in wanted:
            continue
        chunk_id = chunk.get("chunk_id")
        if not isinstance(chunk_id, str):
            continue
        out.append(
            Candidate(
                evidence_id=f"{doc_id}:{chunk_id}",
                text=str(chunk.get("text", "")),
                page=page,
                score=_NOTE_SCORE,
                source_tool="note",
            )
        )
    return out


def _note_pages_for_unit(note_map: NoteMap | None, unit: UnitSpec) -> list[int]:
    """Union of the note pages this unit's fields reference.

    A taxonomy leaf ("Buildings") rarely matches a report label, so each field
    also carries its parent concept's report-side aliases ("property, plant and
    equipment") -- without that bridge only 4 of 402 v4.1 leaves resolved a note
    at all. Every alias contributes: a concept legitimately spans more than one
    note (advances sit in "other assets", interest receivable in "other
    financial assets"), so taking only the first match drops real evidence."""
    if note_map is None:
        return []
    pages: list[int] = []
    seen: set[int] = set()
    for field in unit.fields:
        for label in (field.field_name, *field.note_aliases):
            for page in note_map.pages_for_label(label):
                if page not in seen:
                    seen.add(page)
                    pages.append(page)
    return pages


async def _pool_for(
    *, ctx: ExtractContext, query: str, k: int
) -> list[Candidate]:
    """hybrid_search -> Candidate list, scoped to this document/version.
    Never raises (decision 10): a failed search degrades to an empty pool.
    An unconfigured embedder/vector_store degrades the same way -- hybrid_
    search now requires both non-None, so there is nothing to call."""
    pool, _error = await _pool_checked(ctx=ctx, query=query, k=k)
    return pool


async def _pool_checked(
    *, ctx: ExtractContext, query: str, k: int
) -> tuple[list[Candidate], str | None]:
    """`_pool_for`, but saying WHY a pool is empty: (candidates, error).
    An unconfigured or failing retrieval is an error, not "nothing found" --
    storing it as a not-found read would cache a null that no later run
    would ever retry. Never raises."""
    if ctx.embedder is None or ctx.vector_store is None:
        return [], "retrieval is not configured (no embedder or vector store)"
    hits: list[dict[str, object]] = []
    if query.strip():
        try:
            hits = await hybrid_search(
                session=ctx.session, embedder=ctx.embedder,
                vector_store=ctx.vector_store, query=query.strip(), k=k,
                filters={"doc_key": ctx.doc_key, "version": ctx.version},
            )
        except Exception as exc:  # noqa: BLE001 - decision 10
            log.warning("extractor.search_failed", doc_id=ctx.doc_id, error=repr(exc))
            if isinstance(exc, SQLAlchemyError):
                # A failed statement poisons the shared session for every later
                # group; a vector-store error leaves it clean (and a needless
                # rollback would expire the caller's ORM objects).
                with contextlib.suppress(Exception):
                    await ctx.session.rollback()
            return [], f"retrieval failed: {type(exc).__name__}: {exc}"
    return _candidates(hits), None


def _candidates(hits: Sequence[Mapping[str, object]]) -> list[Candidate]:
    return [
        Candidate(
            evidence_id=f"{h['doc_id']}:{h['chunk_id']}",
            text=str(h.get("text", "")),
            page=cast(int | None, h.get("page")),
            score=float(cast(float, h.get("score", 0.0))),
            source_tool="hybrid",
        )
        for h in hits
    ]


def _basis_keep(cand_basis: str, preference: str) -> bool:
    """True when a candidate tagged `cand_basis` should be kept for a unit
    whose `basis_preference` is `preference` (TR-1 P4b, design spec sec 6.2).

    `"both"` keeps everything. Otherwise a candidate is kept when its basis
    MATCHES the preference or is `"unknown"` (front-matter/unrecognized
    tables serve either basis) and dropped only when it is the OPPOSITE
    basis. Pure string comparison: never raises."""
    if preference == "both":
        return True
    return cand_basis == preference or cand_basis == "unknown"


def _filter_by_basis(
    candidates: Sequence[Candidate],
    *,
    basis_artifact: Mapping[str, object],
    preference: str,
) -> list[Candidate]:
    """Drop candidates whose basis is the OPPOSITE of `preference`, replacing
    the old `consolidated_start_page` single-cutoff filter with the labeled
    basis-region model (TR-1 P4b). `page_basis` degrades a missing/malformed
    `basis_artifact`, or a candidate with `page=None`, to `"unknown"` --
    which `_basis_keep` always keeps -- so a missing basis artifact serves
    every candidate rather than silently dropping half the document.

    GRACEFUL FALLBACK: if filtering to `preference` would drop EVERY candidate
    -- the preferred basis is entirely absent from the pool, e.g. a
    consolidated-only integrated report against the default
    `standalone` preference -- keep the unfiltered candidates instead of
    starving the unit. Extracting from the only basis present beats returning
    nothing. Never raises."""
    kept = [
        c for c in candidates
        if _basis_keep(page_basis(basis_artifact, c.page), preference)
    ]
    if kept:
        return kept
    # MEASURED: refusing the fallback on a two-basis document (returning [] so a
    # unit could never read the wrong statement set) starves it instead. On TCS
    # it dropped correct fields 19/24 -> 9/24 and filled 125 -> 71, taking every
    # headline total to null -- strictly worse than the mixing it prevented.
    # Retrieval, not this filter, is the place to enforce basis: the pool has to
    # arrive basis-correct. Until it does, extracting from the only candidates a
    # unit has beats returning nothing.
    return list(candidates)


_TABLE_CARD_RE = re.compile(r"^t(\d+)$")


def _card_identity(text: str) -> str:
    """The table card's identity line (statement_type | basis=... | page),
    reusing it verbatim rather than reconstructing one: a card's TEXT always
    opens with `[...]` (see fdi.core.retrieval._table_chunks). Strips the
    surrounding brackets when present; a malformed/bracket-less first line is
    returned as-is. Total: never raises on any input shape (an empty `text`
    yields "")."""
    first_line = text.split("\n", 1)[0].strip()
    if len(first_line) >= 2 and first_line.startswith("[") and first_line.endswith("]"):
        return first_line[1:-1]
    return first_line


def _render_table_candidate(
    candidate: Candidate, *, structure: Mapping[str, object]
) -> Candidate:
    """Render a TABLE CARD candidate's `text` as a markdown table fetched
    from `structure["tables"][table_index]["grid"]` (TR-1 P3-wire, design
    spec sec 5.3) -- `table_index` parsed out of its `t{i}` chunk_id
    (evidence_id `f"{doc_id}:t{i}"`). A section-chunk candidate (`s{i}`), an
    out-of-range/missing `table_index`, a missing/malformed grid, or an empty
    render all leave `candidate` UNCHANGED -- the card's own LOCATION-summary
    text still reaches the LLM rather than being lost. `Candidate` is frozen,
    so a rendered candidate is a NEW instance via `dataclasses.replace`.
    Total: never raises (decision 10)."""
    chunk_id = candidate.evidence_id.split(":", 1)[1] if ":" in candidate.evidence_id else ""
    match = _TABLE_CARD_RE.match(chunk_id)
    if match is None:
        return candidate

    table_index = int(match.group(1))
    tables_raw = structure.get("tables") if isinstance(structure, Mapping) else None
    tables = tables_raw if isinstance(tables_raw, list) else []
    if not (0 <= table_index < len(tables)):
        return candidate

    table = tables[table_index]
    if not isinstance(table, Mapping):
        return candidate
    grid = table.get("grid")
    if not isinstance(grid, Mapping):
        return candidate

    rendered = render_table_markdown(repair_headers(grid), identity=_card_identity(candidate.text))
    if not rendered:
        return candidate
    return replace(candidate, text=rendered)


async def extract_unit(
    *,
    ctx: ExtractContext,
    unit: UnitSpec,
    client: ExtractorClient,
    basis_artifact: Mapping[str, object] | None = None,
    structure: Mapping[str, object] | None = None,
    note_map: NoteMap | None = None,
    chunks: Sequence[Mapping[str, object]] | None = None,
    effective_basis: str = "",
) -> list[dict[str, object]]:
    """ONE retrieval + ONE LLM call for every field in `unit` -- schema v3.1's
    "Retrieve-once, extract-many". Returns one output_contract record per
    field, in unit order. Never raises (decision 10): a failed search or
    client call degrades THIS unit's fields to well-formed null records and
    never affects another unit."""
    read = await _read_unit(
        ctx=ctx, unit=unit, client=client, basis_artifact=basis_artifact,
        structure=structure, note_map=note_map, chunks=chunks,
        effective_basis=effective_basis,
    )
    return read.records


@dataclass(frozen=True)
class _UnitRead:
    """One unit's records plus what `extract_unit` hides: whether the model
    was asked at all, and the error that stopped it (retrieval or the call)."""

    records: list[dict[str, object]]
    asked: bool
    error: str | None


async def _read_unit(
    *,
    ctx: ExtractContext,
    unit: UnitSpec,
    client: ExtractorClient,
    basis_artifact: Mapping[str, object] | None,
    structure: Mapping[str, object] | None,
    note_map: NoteMap | None,
    chunks: Sequence[Mapping[str, object]] | None,
    effective_basis: str,
) -> _UnitRead:
    top_k = unit_top_k(len(unit.fields))
    pool, pool_error = await _pool_checked(
        ctx=ctx, query=unit.retrieval_query, k=top_k * _POOL_OVERFETCH
    )

    scoped = filter_candidates(
        pool, consolidated_start_page=None, needs_tables=unit_needs_tables(unit),
    )
    # Basis is a DOCUMENT-level decision resolved once by extract_document, not
    # a per-unit one: units that each fall back independently mix statement sets.
    scoped = _filter_by_basis(
        scoped, basis_artifact=basis_artifact or {},
        preference=effective_basis or unit.basis_preference,
    )
    # Note-follow is injected AFTER the filters: these pages were resolved from a
    # printed reference, so no similarity/basis heuristic may discard them.
    note_pages = _note_pages_for_unit(note_map, unit)
    if note_pages:
        have = {c.evidence_id for c in scoped}
        injected = [
            c
            for c in _note_candidates(chunks or [], note_pages, ctx.doc_id)
            if c.evidence_id not in have
        ][:_NOTE_MAX]
        if injected:
            log.info(
                "extractor.note_follow",
                unit=unit.unit_id, pages=len(note_pages), injected=len(injected),
            )
            scoped = injected + list(scoped)
    ranked = rerank_candidates(scoped, unit.locator_keywords, top_k=top_k)
    if not ranked:
        # Note-follow can still have found pages when search failed; only a
        # unit left with NOTHING to read reports the retrieval error.
        return _UnitRead(assemble_unit_results(unit, {}, ranked), asked=False,
                         error=pool_error)

    prompt_candidates = [
        _render_table_candidate(c, structure=structure or {}) for c in ranked
    ]
    system, user = build_unit_messages(unit, prompt_candidates)
    raw: Mapping[str, object] = {}
    usage: LlmCallUsage | None = None
    call_error: str | None = None
    try:  # never raise (decision 10)
        result = client.extract(system=system, user=user)
        raw = result.data if isinstance(result.data, Mapping) else {}
        usage = result.usage
    except Exception as exc:  # noqa: BLE001 - decision 10
        call_error = f"extraction call failed: {type(exc).__name__}: {exc}"
        log.warning("extractor.call_failed", doc_id=ctx.doc_id, unit=unit.unit_id,
                    error=repr(exc))
    # Usage bookkeeping gets its own guard AND its own rollback, deliberately.
    # A failed write here does not lose `raw` (the assignment above already
    # happened) -- it poisons ctx.SESSION, and hybrid_search shares that
    # session. So one failed telemetry insert silently killed retrieval for
    # every LATER unit in the document. Measured with llm_usage_event absent:
    # TCS fell 125 -> 71 fields filled and 19 -> 9 correct, deterministically,
    # with the early units fine and the rest empty. Costing is bookkeeping; it
    # must never be able to take extraction down with it.
    if usage is not None:
        try:
            await _record_llm_usage(ctx=ctx, source="extract_document", usage=usage)
        except Exception:  # noqa: BLE001 - decision 10
            log.info("extractor.usage_record_failed", doc_id=getattr(ctx, "doc_id", ""))
            with contextlib.suppress(Exception):
                await ctx.session.rollback()
    return _UnitRead(assemble_unit_results(unit, raw, ranked), asked=True, error=call_error)


async def extract_field(
    *, ctx: ExtractContext, field: FieldSpec, client: ExtractorClient
) -> dict[str, object]:
    """Single-field extraction, preserved for callers that hold one FieldSpec.
    Pools with the single-field constants (_POOL_K / _EXTRACT_TOP_K) and the
    single-field prompt, exactly as before -- it is NOT routed through
    extract_unit, so its behaviour is unchanged. Never raises."""
    pool = await _pool_for(ctx=ctx, query=field.retrieval_query, k=_POOL_K)
    ranked = rerank_candidates(pool, field.keywords, top_k=_EXTRACT_TOP_K)
    if not ranked:
        return assemble_result(field, {}, ranked)

    system, user = build_extraction_messages(field, ranked)
    raw: Mapping[str, object] = {}
    usage: LlmCallUsage | None = None
    with contextlib.suppress(Exception):  # never raise (decision 10)
        result = client.extract(system=system, user=user)
        raw = result.data
        usage = result.usage
    # Usage bookkeeping gets its own guard AND its own rollback, deliberately.
    # A failed write here does not lose `raw` (the assignment above already
    # happened) -- it poisons ctx.SESSION, and hybrid_search shares that
    # session. So one failed telemetry insert silently killed retrieval for
    # every LATER unit in the document. Measured with llm_usage_event absent:
    # TCS fell 125 -> 71 fields filled and 19 -> 9 correct, deterministically,
    # with the early units fine and the rest empty. Costing is bookkeeping; it
    # must never be able to take extraction down with it.
    if usage is not None:
        try:
            await _record_llm_usage(ctx=ctx, source="extract_document", usage=usage)
        except Exception:  # noqa: BLE001 - decision 10
            log.info("extractor.usage_record_failed", doc_id=getattr(ctx, "doc_id", ""))
            with contextlib.suppress(Exception):
                await ctx.session.rollback()
    return assemble_result(field, raw, ranked)


async def _read_latest_artifact(
    ctx: ExtractContext, stage: str
) -> Mapping[str, object]:
    """The document's latest `stage` artifact body, or `{}` if there is no
    completed run, no artifact key, or the read fails. Never raises
    (decision 10): geometry is an enrichment, and a dead MinIO must degrade
    the highlight, not the document's whole extraction batch."""
    body: Mapping[str, object] = {}
    with contextlib.suppress(Exception):
        run = await RegistryRepository(ctx.session).latest_completed_run(
            ctx.doc_id, stage
        )
        if run is not None and run.artifact_key is not None:
            raw = await ctx.store.get_json(run.artifact_key)
            if isinstance(raw, Mapping):
                body = raw
    return body


def _attach_regions(
    record: dict[str, object],
    field: FieldSpec,
    *,
    doc_id: str,
    provenance: Mapping[str, object],
    structure: Mapping[str, object],
) -> dict[str, object]:
    """Add `source.bounding_regions` to one assemble_result record, as
    `{current: [...], prior: [...]}` -- cell-precise when `field`'s keywords
    match a table row (fdi.core.provenance.resolve_field_regions), falling
    back to the field's whole-table/whole-section geometry
    (resolve_evidence_regions) under `current` with `prior: []` otherwise.
    Never raises."""
    source = record.get("source")
    if not isinstance(source, dict):
        return record
    evidence_ids = source.get("evidence_ids")
    ids: Sequence[object] = evidence_ids if isinstance(evidence_ids, list) else []
    fallback = resolve_evidence_regions(doc_id, ids, provenance, structure)
    source["bounding_regions"] = resolve_field_regions(
        keywords=field.keywords, evidence_ids=ids, structure=structure,
        fallback_current=fallback,
    )
    return record


async def _read_chunks_for(ctx: ExtractContext) -> list[Mapping[str, object]]:
    """The document's chunk records, once. Never raises: no chunks -> no
    note-follow, and retrieval degrades to search alone."""
    try:
        return list(await read_chunks(RegistryRepository(ctx.session), ctx.store, ctx.doc_id))
    except Exception:  # noqa: BLE001 - decision 10
        log.info("extractor.chunks_unavailable", doc_id=ctx.doc_id)
        return []


def _face_pages(tables: object, page_lo: int, page_hi: int) -> list[int]:
    """Pages holding a statement face, found by scoring table CONTENT."""
    if not isinstance(tables, list):
        return []
    pages: list[int] = []
    for statement in STATEMENT_ANCHORS:
        for found in find_statement_tables(tables, statement, page_lo, page_hi):
            if found.page not in pages:
                pages.append(found.page)
    return sorted(pages)


async def _note_map_for(
    ctx: ExtractContext,
    *,
    basis_artifact: Mapping[str, object],
    preference: str,
) -> NoteMap | None:
    """Build the note index for ONE basis region.

    Scoping to a basis is not optional: a report carrying both consolidated and
    standalone statements has two "note 8", and an unscoped index resolves a
    reference to whichever came first. The AGM notice that follows the
    statements restarts numbering too, so `find_statement_pages` trims the tail.
    Never raises -- a missing OCR artifact degrades to no note-follow."""
    ocr = await _read_latest_artifact(ctx, "ocr")
    paragraphs = ocr.get("paragraphs")
    tables = ocr.get("tables")
    if not isinstance(paragraphs, list) or not paragraphs:
        return None
    lo, hi = basis_bounds(basis_artifact, preference)
    pages = find_statement_pages(paragraphs)
    if lo == 0 and hi == 0:
        lo, hi = pages.page_lo, pages.page_hi
    elif pages.page_hi:
        hi = min(hi, pages.page_hi)
    if lo <= 0 or hi < lo:
        return None
    # Which pages carry a FACE is decided by what the tables say, not by what a
    # heading claims. Heading matching returned five pages for TCS -- three of
    # them contents/segment pages that carry no face -- and could not say which
    # statement it had found; scoring a table against the line names Schedule III
    # mandates returns exactly the consolidated and standalone faces, typed.
    # Headings remain the fallback when no table scores (scanned or odd layouts).
    face_pages = _face_pages(tables, lo, hi) or [
        p for p in pages.face_pages if lo <= p <= hi
    ]
    note_map = build_note_map(
        paragraphs,
        tables if isinstance(tables, list) else [],
        face_pages=face_pages,
        page_lo=lo,
        page_hi=hi,
    )
    log.info(
        "extractor.note_map_built",
        doc_id=ctx.doc_id, basis=preference, notes=len(note_map.by_ref),
        labels=len(note_map.by_label), unresolved=len(note_map.unresolved),
    )
    return note_map


async def _basis_artifact_for(ctx: ExtractContext) -> Mapping[str, object]:
    """Read this document's `basis` artifact ONCE (TR-1 P4b). Never raises:
    `_read_latest_artifact` already degrades a missing run/artifact_key/
    unreadable body to `{}`, and `page_basis` treats an empty/malformed
    artifact as 'every page is unknown' -- which `_basis_keep` always keeps,
    so extraction proceeds unscoped by basis rather than failing. Logged
    here (pipeline layer; fdi.core stays log-free) so an absent artifact is
    distinguishable from one with `regions: []` (a document with no
    standalone/consolidated anchors, legitimately unscoped)."""
    basis = await _read_latest_artifact(ctx, "basis")
    if not basis:
        log.info("extractor.basis_artifact_missing", doc_id=ctx.doc_id)
        return basis
    regions = basis.get("regions")
    log.info(
        "extractor.basis_artifact_found",
        doc_id=ctx.doc_id,
        region_count=len(regions) if isinstance(regions, list) else 0,
    )
    return basis


# ---------------------------------------------------------------------------
# The engine's one entry point (S3): extract these Fields from this Report.
#
# `extract_fields` plans (fdi.core.extraction_plan -- the only place staleness
# is decided), reuses every fresh read, reads ONLY the stale Fields (one
# retrieval + one call per group, carrying just those Fields), and stores each
# group's reads as soon as that group finishes -- so a crashed run keeps what
# it paid for, and its redelivery reuses it instead of paying twice.
# ---------------------------------------------------------------------------

RETRIEVAL_CONFIG_VERSION = "1"
"""Bump when pooling, filtering, reranking, note-follow or table rendering
changes what context a group's call receives: every read becomes stale
(ADR-0004). Field-independent prompt wording is versioned automatically
(`fdi.core.extractor.prompt_version`)."""

_NOT_ANSWERED = "the model returned no answer for this Field"


def engine_versions(client: ExtractorClient) -> EngineVersions:
    """The engine half of every read's cache key, for `client`."""
    model = getattr(client, "model_id", None)
    return EngineVersions(
        prompt_version=prompt_version(),
        retrieval_version=RETRIEVAL_CONFIG_VERSION,
        model=model if isinstance(model, str) and model else type(client).__name__,
    )


@dataclass(frozen=True)
class EngineField:
    """One Field as the engine reads it: identity, definition hash and the
    retrieval/prompt spec built from its definition."""

    field_id: str
    definition_hash: str
    spec: FieldSpec


@dataclass(frozen=True)
class FieldResult:
    """What `extract_fields` produced for one Field: the live read it now
    points at, whether it was reused or read now, and why (plan reason)."""

    field_id: str
    read: FieldRead
    action: str
    reason: str


def _read_status(record: Mapping[str, object], error: str | None) -> str:
    if error is not None:
        return "failed"
    value = record.get("value")
    if value is None:
        return "not_found"
    if isinstance(value, Mapping) and value and all(v is None for v in value.values()):
        return "not_found"
    return "extracted"


def _confidence(record: Mapping[str, object]) -> float | None:
    c = record.get("confidence")
    return float(c) if isinstance(c, int | float) and not isinstance(c, bool) else None


async def extract_fields(
    *,
    ctx: ExtractContext,
    fields: Sequence[EngineField],
    client: ExtractorClient,
    forced: Collection[str] = (),
    run_id: str | None = None,
) -> list[FieldResult]:
    """Extract `fields` from the Report `ctx` names; one result per Field in
    input order (a repeated Field id is answered once per occurrence).

    Fresh reads are reused at no cost. Stale ones are read now and stored as
    immutable `field_read` rows (committed group by group); a failed read is
    stored too, as `failed`, so the next run retries it. `forced` Field ids
    are read again even when fresh; their previous read is kept, superseded.
    Never raises for a retrieval or model failure -- those become `failed`
    reads; database errors propagate (nothing could be stored anyway).
    Derived Fields are refused: they are computed when shown, never read."""
    derived = [f.field_id for f in fields if f.spec.origin == "derived"]
    if derived:
        raise ValueError(f"extract_fields was given derived Fields: {derived[:5]}")
    engine = engine_versions(client)
    unique: dict[str, EngineField] = {}
    for f in fields:
        unique.setdefault(f.field_id, f)
    repo = ExtractionRepository(ctx.session)
    stored = await repo.live_reads([ctx.doc_id], list(unique))
    plan = plan_extraction(
        fields=[PlannedField(f.field_id, f.definition_hash, f.spec.origin)
                for f in unique.values()],
        doc_ids=[ctx.doc_id],
        reads=[_stored_read(r) for r in stored],
        engine=engine,
        forced=forced,
    )
    by_id = {r.id: r for r in stored}
    results: dict[str, FieldResult] = {}
    for item in plan.reused():
        results[item.field_id] = FieldResult(
            item.field_id, by_id[str(item.read_id)], "reuse", item.reason
        )
    stale = {i.field_id: i for i in plan.to_extract()}
    if stale:
        log.info("extractor.plan", doc_id=ctx.doc_id, reused=len(results),
                 extract=len(stale), reasons=dict(summarize(plan)))
        await _read_stale(
            ctx=ctx, repo=repo, fields=[unique[fid] for fid in stale], plan=stale,
            client=client, engine=engine, run_id=run_id, out=results,
        )
        # A rollback while reading (a failed search or usage insert) expires
        # every instance in the session, including the reused reads loaded
        # before the loop; touching them would lazy-load under asyncio. Load
        # them again by id (ids come from the plan, never from those rows).
        reused_ids = {i.field_id: str(i.read_id) for i in plan.reused()}
        if reused_ids:
            fresh = await repo.reads_by_ids(reused_ids.values())
            for fid, rid in reused_ids.items():
                results[fid] = FieldResult(fid, fresh[rid], "reuse", "fresh")
    return [results[f.field_id] for f in fields]


def _stored_read(r: FieldRead) -> StoredRead:
    return StoredRead(
        read_id=r.id, doc_id=r.doc_id, field_id=r.field_id,
        definition_hash=r.definition_hash, prompt_version=r.prompt_version,
        retrieval_version=r.retrieval_version, model=r.model, status=r.status,
    )


async def _read_stale(
    *,
    ctx: ExtractContext,
    repo: ExtractionRepository,
    fields: Sequence[EngineField],
    plan: Mapping[str, PlanItem],
    client: ExtractorClient,
    engine: EngineVersions,
    run_id: str | None,
    out: dict[str, FieldResult],
) -> None:
    provenance = await _read_latest_artifact(ctx, "provenance")
    structure = await _read_latest_artifact(ctx, "structure")
    basis_artifact = await _basis_artifact_for(ctx)
    units = group_fields_by_unit([f.spec for f in fields])
    preferred = units[0].basis_preference if units else "standalone"
    effective_basis = resolve_effective_basis(basis_artifact, preferred)
    chunks = list(await _read_chunks_for(ctx))
    note_maps: dict[str, NoteMap | None] = {}
    by_spec = {id(f.spec): f for f in fields}
    for unit in units:
        preference = effective_basis or unit.basis_preference
        if preference not in note_maps:
            note_maps[preference] = await _note_map_for(
                ctx, basis_artifact=basis_artifact, preference=preference
            )
        read = await _read_unit(
            ctx=ctx, unit=unit, client=client, basis_artifact=basis_artifact,
            structure=structure, note_map=note_maps[preference], chunks=chunks,
            effective_basis=effective_basis,
        )
        stored: list[tuple[EngineField, str]] = []
        for spec, record in zip(unit.fields, read.records, strict=True):
            f = by_spec[id(spec)]
            record = _attach_regions(record, spec, doc_id=ctx.doc_id,
                                     provenance=provenance, structure=structure)
            error = read.error
            if error is None and read.asked and not record.get("answered", True):
                error = _NOT_ANSWERED
            key = cache_key(f.field_id, f.definition_hash, engine)
            if plan[f.field_id].reason in ("forced", "previously_failed"):
                await repo.supersede(doc_id=ctx.doc_id, field_id=f.field_id, cache_key=key)
            source = record.get("source")
            read_id = await repo.insert_read({
                "doc_id": ctx.doc_id, "field_id": f.field_id, "cache_key": key,
                "definition_hash": f.definition_hash,
                "prompt_version": engine.prompt_version,
                "retrieval_version": engine.retrieval_version, "model": engine.model,
                "status": _read_status(record, error),
                "value": None if error is not None else record.get("value"),
                "confidence": None if error is not None else _confidence(record),
                "basis": effective_basis or unit.basis_preference,
                "provenance": dict(source) if isinstance(source, Mapping) else {},
                "answered": bool(record.get("answered", read.asked)),
                "error": error, "run_id": run_id,
            })
            stored.append((f, read_id))
        await ctx.session.commit()
        rows = await repo.reads_by_ids(rid for _f, rid in stored)
        for f, read_id in stored:
            out[f.field_id] = FieldResult(
                f.field_id, rows[read_id], "extract", plan[f.field_id].reason
            )


# ---------------------------------------------------------------------------
# POST /extract/document: the configured schema document through the engine.
# ---------------------------------------------------------------------------

# Identity of a Field of the configured schema DOCUMENT (not a stored
# Extraction schema): derived from its path, so the same document Field keeps
# its cached reads across requests. Never change it.
_DOCUMENT_FIELD_NAMESPACE = uuid.UUID("9d1c7b52-0e4f-4a77-8a51-3c6f0b8e2d94")


def document_engine_field(spec: FieldSpec) -> EngineField:
    """An `EngineField` for a Field of the configured schema document."""
    identity = spec.field_path or f"{spec.unit_id}/{spec.field_name}"
    body = json.dumps(
        {
            "name": spec.field_name, "data_type": spec.data_type,
            "query": spec.retrieval_query, "keywords": list(spec.keywords),
            "prompt": spec.extraction_prompt, "enum": list(spec.enum_values or ()),
            "unit": [spec.unit_id, spec.unit_title, spec.basis_preference,
                     list(spec.locator_keywords)],
            "periods": list(spec.periods), "tag": spec.tag,
            "note_aliases": list(spec.note_aliases),
        },
        sort_keys=True, ensure_ascii=False,
    )
    return EngineField(
        field_id=str(uuid.uuid5(_DOCUMENT_FIELD_NAMESPACE, identity)),
        definition_hash=hashlib.sha256(body.encode("utf-8")).hexdigest(),
        spec=spec,
    )


def read_record(spec: FieldSpec, read: FieldRead) -> dict[str, object]:
    """A stored read in the output_contract record shape the extraction
    record and `/extract/document` have always carried."""
    source = dict(read.provenance) if isinstance(read.provenance, Mapping) else {}
    source.setdefault("page", None)
    source.setdefault("snippet", None)
    source.setdefault("evidence_ids", [])
    source.setdefault("bounding_regions", {"current": [], "prior": []})
    return {
        "field_name": spec.field_name, "tag": spec.tag, "origin": spec.origin,
        "value": read.value, "source": source,
        "confidence": read.confidence if read.confidence is not None else 0.0,
        "answered": read.answered,
    }


async def extract_document(
    *,
    ctx: ExtractContext,
    fields: Sequence[FieldSpec],
    client: ExtractorClient,
    force: bool = False,
) -> dict[str, object]:
    """Extract every Field of a schema DOCUMENT (the configured one, for
    `POST /extract/document` and the factsheet) through `extract_fields`, in
    the caller's field order. Response shape unchanged.

    Cached per Field, not per document: a later request for the same document
    Fields is served from the stored reads with no model call, adding one
    Field reads only that Field, and rewording a Field re-reads only it. The
    old cache was keyed by a hash of Field NAMES, so it did the opposite on
    both counts. `force` reads every Field again.

    The extraction record is still written to the artifact store for the
    readers that load it (`score_pipeline.py`); it is no longer read back as
    a cache."""
    engine_fields = [document_engine_field(f) for f in fields]
    results = await extract_fields(
        ctx=ctx, fields=engine_fields, client=client,
        forced=[f.field_id for f in engine_fields] if force else (),
    )
    records = [read_record(f, r.read) for f, r in zip(fields, results, strict=True)]
    record = build_extraction_record(
        doc_id=ctx.doc_id, doc_key=ctx.doc_key, version=ctx.version, results=records,
    )
    key = await _persist_extraction(ctx=ctx, fields=fields, record=record)
    return {
        "schema_version": 1, "doc_id": ctx.doc_id,
        "field_count": len(records), "fields": records,
        "extraction_key": key,
    }


async def _persist_extraction(
    *, ctx: ExtractContext, fields: Sequence[FieldSpec], record: Mapping[str, object]
) -> str | None:
    """Write the extraction record to the artifact store; return its key, or
    None if the write failed.

    Guarded like every other I/O step in this module (decision 10): a document
    whose extraction SUCCEEDED must still be returned to the caller when the
    object store is merely unreachable. Losing the durable copy is a
    degradation to log, not a reason to discard 200 fields the LLM already
    paid for."""
    schema_hash = output_hash([f.field_path or f.field_name for f in fields])
    key = extraction_key(ctx.doc_id, EXTRACTION_RECORD_VERSION, schema_hash)
    try:
        await ctx.store.put_json(key, record)
    except Exception as exc:  # noqa: BLE001
        log.warning(
            "extractor.persist_failed",
            doc_id=ctx.doc_id, key=key, error=str(exc),
        )
        return None
    log.info(
        "extractor.persisted",
        doc_id=ctx.doc_id, key=key,
        field_count=record.get("field_count"),
        non_null_count=record.get("non_null_count"),
    )
    return key
