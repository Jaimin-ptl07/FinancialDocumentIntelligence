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
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, replace
from typing import Protocol, cast, runtime_checkable

import structlog
from sqlalchemy.ext.asyncio import AsyncSession

from fdi.core.basis import page_basis
from fdi.core.extractor import (
    EXTRACTION_RECORD_VERSION,
    Candidate,
    FieldSpec,
    UnitSpec,
    assemble_result,
    assemble_unit_results,
    build_extraction_messages,
    build_extraction_record,
    build_unit_messages,
    filter_candidates,
    group_fields_by_unit,
    rerank_candidates,
    unit_needs_tables,
    unit_top_k,
)
from fdi.core.provenance import resolve_evidence_regions
from fdi.core.table_render import render_table_markdown, repair_headers
from fdi.pipeline.embeddings import EmbeddingClient
from fdi.pipeline.keys import extraction_key, output_hash
from fdi.pipeline.retrieval import hybrid_search
from fdi.pipeline.vectorstore import VectorStore
from fdi.platform.db.repository import RegistryRepository
from fdi.platform.storage.artifact_store import ArtifactStore

log = structlog.get_logger(__name__)


@runtime_checkable
class ExtractorClient(Protocol):
    def extract(self, *, system: str, user: str) -> Mapping[str, object]: ...


class FakeExtractorClient:
    """Replays scripted {value, source, confidence}-shaped dicts, one per
    call, in order -- mirrors fdi.pipeline.agent.FakeAgentClient exactly
    (same AssertionError-on-overcall posture, a loud test-diagnosable
    failure rather than a confusing IndexError deep inside extract_field)."""

    def __init__(self, results: list[Mapping[str, object]]) -> None:
        self._results = results
        self._next = 0
        self.calls: list[tuple[str, str]] = []

    def extract(self, *, system: str, user: str) -> Mapping[str, object]:
        self.calls.append((system, user))
        if self._next >= len(self._results):
            raise AssertionError(
                f"FakeExtractorClient exhausted: {len(self._results)} scripted "
                f"result(s), call {len(self.calls)} requested"
            )
        result = self._results[self._next]
        self._next += 1
        return result


@dataclass
class ExtractContext:
    session: AsyncSession
    store: ArtifactStore
    embedder: EmbeddingClient | None
    vector_store: VectorStore | None
    doc_id: str
    doc_key: str
    version: int


_POOL_K = 60
_EXTRACT_TOP_K = 20

# The pool is over-fetched RELATIVE to top_k, not fixed. The filters run
# BETWEEN pooling and reranking, so a fixed 60 against an adaptive top_k of up
# to 60 would leave large units (RU_BS) with fewer post-filter candidates than
# top_k -- strictly less context than the harness supplied, a silent recall
# regression. top_k*2 mirrors the harness's fuse(k=top_k*2).
_POOL_OVERFETCH = 2


async def _pool_for(
    *, ctx: ExtractContext, query: str, k: int
) -> list[Candidate]:
    """hybrid_search -> Candidate list, scoped to this document/version.
    Never raises (decision 10): a failed search degrades to an empty pool.
    An unconfigured embedder/vector_store degrades the same way -- hybrid_
    search now requires both non-None, so there is nothing to call."""
    if ctx.embedder is None or ctx.vector_store is None:
        return []
    hits: list[dict[str, object]] = []
    if query.strip():
        with contextlib.suppress(Exception):
            hits = await hybrid_search(
                session=ctx.session, embedder=ctx.embedder,
                vector_store=ctx.vector_store, query=query.strip(), k=k,
                filters={"doc_key": ctx.doc_key, "version": ctx.version},
            )
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
    consolidated-only integrated report (Reliance) against the default
    `standalone` preference -- keep the unfiltered candidates instead of
    starving the unit. Extracting from the only basis present beats returning
    nothing. Never raises."""
    kept = [
        c for c in candidates
        if _basis_keep(page_basis(basis_artifact, c.page), preference)
    ]
    return kept if kept else list(candidates)


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
) -> list[dict[str, object]]:
    """ONE retrieval + ONE LLM call for every field in `unit` -- schema v3.1's
    "Retrieve-once, extract-many". Returns one output_contract record per
    field, in unit order. Never raises (decision 10): a failed search or
    client call degrades THIS unit's fields to well-formed null records and
    never affects another unit."""
    top_k = unit_top_k(len(unit.fields))
    pool = await _pool_for(ctx=ctx, query=unit.retrieval_query, k=top_k * _POOL_OVERFETCH)

    scoped = filter_candidates(
        pool, consolidated_start_page=None, needs_tables=unit_needs_tables(unit),
    )
    scoped = _filter_by_basis(
        scoped, basis_artifact=basis_artifact or {}, preference=unit.basis_preference,
    )
    ranked = rerank_candidates(scoped, unit.locator_keywords, top_k=top_k)
    if not ranked:
        return assemble_unit_results(unit, {}, ranked)

    prompt_candidates = [
        _render_table_candidate(c, structure=structure or {}) for c in ranked
    ]
    system, user = build_unit_messages(unit, prompt_candidates)
    raw: Mapping[str, object] = {}
    with contextlib.suppress(Exception):  # never raise (decision 10)
        raw = client.extract(system=system, user=user)
    return assemble_unit_results(unit, raw, ranked)


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
    with contextlib.suppress(Exception):  # never raise (decision 10)
        raw = client.extract(system=system, user=user)
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
        run = await RegistryRepository(ctx.session).latest_run(ctx.doc_id, stage)
        if run is not None and run.artifact_key is not None:
            raw = await ctx.store.get_json(run.artifact_key)
            if isinstance(raw, Mapping):
                body = raw
    return body


def _attach_regions(
    record: dict[str, object],
    *,
    doc_id: str,
    provenance: Mapping[str, object],
    structure: Mapping[str, object],
) -> dict[str, object]:
    """Add `source.bounding_regions` to one assemble_result record.

    Always a list -- [] when unresolvable -- so the UI branches on one shape,
    matching assemble_result's own habit of returning `evidence_ids: []`
    rather than None. Never raises."""
    source = record.get("source")
    if not isinstance(source, dict):
        return record
    evidence_ids = source.get("evidence_ids")
    ids: Sequence[object] = evidence_ids if isinstance(evidence_ids, list) else []
    source["bounding_regions"] = resolve_evidence_regions(
        doc_id, ids, provenance, structure
    )
    return record


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


async def _cached_extraction(
    ctx: ExtractContext, fields: Sequence[FieldSpec]
) -> dict[str, object] | None:
    """The persisted record for this exact (doc_id, schema) pair, or None if
    absent/unreadable. Never raises (decision 10): a corrupt or unreachable
    cache degrades to a fresh extraction, not a 500. Keyed identically to
    `_persist_extraction`'s write, so a cache write and a cache check always
    agree on where to look."""
    schema_hash = output_hash([f.field_path or f.field_name for f in fields])
    key = extraction_key(ctx.doc_id, EXTRACTION_RECORD_VERSION, schema_hash)
    with contextlib.suppress(Exception):
        if await ctx.store.exists(key):
            raw = await ctx.store.get_json(key)
            if isinstance(raw, Mapping):
                return dict(raw)
    return None


async def extract_document(
    *,
    ctx: ExtractContext,
    fields: Sequence[FieldSpec],
    client: ExtractorClient,
    force: bool = False,
) -> dict[str, object]:
    """Group `fields` into Retrieval Units and run ONE retrieval + ONE LLM
    call per unit, then restore the caller's field order. Signature unchanged
    -- `doc_key`/`module` are still attached by the route. Never raises.

    Unless `force`, a persisted record for this exact (doc_id, schema) pair
    -- written by a prior call's `_persist_extraction` -- is served directly,
    with no retrieval or LLM calls at all: extraction is expensive (one Azure
    OpenAI round trip per Retrieval Unit) and its inputs (the document, the
    schema) do not change between page loads, so re-running on every view
    would burn the same money and the same multi-minute wait for an answer
    already on disk.

    Each record's `source` additionally carries `bounding_regions`: the
    geometry behind its cited chunks, joined by the pure
    fdi.core.provenance.resolve_evidence_regions. All THREE artifacts this
    needs (provenance for geometry, structure for both geometry AND table-
    card rendering, and basis for basis-aware retrieval) are read ONCE here,
    not per field or per unit -- the committed schema has 198 fields, so a
    per-field read would be hundreds of MinIO round trips for one request."""
    if not force:
        cached = await _cached_extraction(ctx, fields)
        if cached is not None:
            log.info("extractor.cache_hit", doc_id=ctx.doc_id)
            return cached

    provenance = await _read_latest_artifact(ctx, "provenance")
    structure = await _read_latest_artifact(ctx, "structure")
    basis_artifact = await _basis_artifact_for(ctx)

    units = group_fields_by_unit(fields)
    flat: list[dict[str, object]] = []
    for unit in units:
        flat.extend(
            await extract_unit(
                ctx=ctx, unit=unit, client=client,
                basis_artifact=basis_artifact, structure=structure,
            )
        )

    # restore input order: group_fields_by_unit is order-preserving, so the
    # flat unit-order list lines up positionally with `unit_order` below. A
    # plain {id(f): i} dict breaks if the SAME FieldSpec object appears twice
    # in `fields` (both entries would map to one record) -- a queue per
    # identity instead assigns each occurrence its own position, in order.
    unit_order: list[FieldSpec] = [f for unit in units for f in unit.fields]
    positions: dict[int, list[int]] = {}
    for i, f in enumerate(unit_order):
        positions.setdefault(id(f), []).append(i)
    results = [flat[positions[id(f)].pop(0)] for f in fields]

    # Geometry is attached AFTER order restoration and BEFORE persistence, so
    # the durable record carries the same bounding_regions the response does.
    results = [
        _attach_regions(
            r, doc_id=ctx.doc_id, provenance=provenance, structure=structure
        )
        for r in results
    ]

    record = build_extraction_record(
        doc_id=ctx.doc_id, doc_key=ctx.doc_key, version=ctx.version, results=results,
    )
    key = await _persist_extraction(ctx=ctx, fields=fields, record=record)

    return {
        "schema_version": 1, "doc_id": ctx.doc_id,
        "field_count": len(results), "fields": results,
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
