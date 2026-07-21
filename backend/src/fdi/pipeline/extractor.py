"""The extractor's orchestrator + ExtractorClient seam (design spec sec
3.2): extract_unit pools context ONCE per Retrieval Unit via hybrid_search
ALONE (dense BGE + keyword Postgres-FTS, RRF-fused -- table content already
reaches this pool as chunks, since build_chunks emits t{i}/t{i}r{j} table
chunks alongside s{i} section chunks and EmbeddingStage embeds all of them;
a separate get_tables table-blob injection was measured to flood the rerank
with same-scored whole-table renders and HURT top-20 recall, 78.8% ->
88.1% once removed -- see docs/architecture/2026-07-19-ingestion-audit-
and-improvement-plan.md), scopes the pool with fdi.core.extractor's two
filters (drop candidates at/after the document's Consolidated-section
boundary; drop whole-table chunks for units with no numeric/list field),
reranks with an adaptive top_k (unit_top_k -- bigger units get a bigger
pool so no field is starved), prompts the client ONCE with every field in
the unit batched into a single call (schema v3.1's "Retrieve-once,
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
NOT routed through extract_unit.

Never raises (decision 10): extract_unit guards every I/O step (hybrid_
search, client.extract) individually, so one UNIT's failure degrades that
unit's fields to well-formed null records, never the whole document's batch
-- extract_field does the same at field granularity for its own callers.

ExtractContext carries doc_id AND doc_key AND version (resolved ONCE by the
caller -- the /extract/document route, which already looks the document up
by doc_key to 404 on an unknown one) rather than just doc_id. hybrid_search's
`filters` are doc_key/version-keyed, not doc_id-keyed; carrying all three on
the context lets extract_field scope hybrid_search to THIS document/version
directly, with no per-field `DocumentRepository.get` lookup inside the loop
-- avoiding an O(fields) redundant single-row lookup per document that
resolving only doc_id would require."""

import contextlib
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Protocol, cast, runtime_checkable

import structlog
from sqlalchemy.ext.asyncio import AsyncSession

from fdi.core.extractor import (
    Candidate,
    FieldSpec,
    UnitSpec,
    assemble_result,
    assemble_unit_results,
    build_extraction_messages,
    build_unit_messages,
    filter_candidates,
    find_consolidated_start_page,
    group_fields_by_unit,
    rerank_candidates,
    unit_needs_tables,
    unit_top_k,
)
from fdi.pipeline.embeddings import EmbeddingClient
from fdi.pipeline.retrieval import hybrid_search, read_chunks
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
    Never raises (decision 10): a failed search degrades to an empty pool."""
    hits: list[dict[str, object]] = []
    if query.strip():
        with contextlib.suppress(Exception):
            hits = await hybrid_search(
                session=ctx.session, store=ctx.store, embedder=ctx.embedder,
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


async def extract_unit(
    *,
    ctx: ExtractContext,
    unit: UnitSpec,
    client: ExtractorClient,
    consolidated_start_page: int | None = None,
) -> list[dict[str, object]]:
    """ONE retrieval + ONE LLM call for every field in `unit` -- schema v3.1's
    "Retrieve-once, extract-many". Returns one output_contract record per
    field, in unit order. Never raises (decision 10): a failed search or
    client call degrades THIS unit's fields to well-formed null records and
    never affects another unit."""
    top_k = unit_top_k(len(unit.fields))
    pool = await _pool_for(ctx=ctx, query=unit.retrieval_query, k=top_k * _POOL_OVERFETCH)

    scoped = filter_candidates(
        pool,
        consolidated_start_page=consolidated_start_page,
        needs_tables=unit_needs_tables(unit),
    )
    ranked = rerank_candidates(scoped, unit.locator_keywords, top_k=top_k)
    if not ranked:
        return assemble_unit_results(unit, {}, ranked)

    system, user = build_unit_messages(unit, ranked)
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


async def _consolidated_start_page(ctx: ExtractContext) -> int | None:
    """Read this document's `chunks` artifact ONCE and derive the boundary.
    Never raises: a missing/unreadable artifact yields None, so extraction
    proceeds unscoped rather than failing -- logged here (pipeline layer;
    fdi.core stays log-free) so a boundary of None is distinguishable from a
    document that legitimately has no Consolidated section vs. one where the
    chunks artifact was missing/unreadable and the whole scoping feature
    silently no-opped."""
    try:
        chunks = await read_chunks(RegistryRepository(ctx.session), ctx.store, ctx.doc_id)
    except Exception as exc:  # noqa: BLE001 -- never raise (decision 10)
        log.warning(
            "extractor.consolidated_boundary_suppressed_exception",
            doc_id=ctx.doc_id, error=repr(exc),
        )
        return None
    boundary = find_consolidated_start_page(chunks)
    # `chunks` is logged on BOTH outcomes: read_chunks returns [] WITHOUT
    # raising when the run or its artifact_key is absent, so chunks=0 with a
    # None boundary means the scoping feature silently no-opped, while a
    # non-zero count with a None boundary means the document genuinely has no
    # Consolidated section. Without the count the two are indistinguishable.
    if boundary is None:
        log.info("extractor.consolidated_boundary_none", doc_id=ctx.doc_id, chunks=len(chunks))
    else:
        log.info(
            "extractor.consolidated_boundary_found",
            doc_id=ctx.doc_id, page=boundary, chunks=len(chunks),
        )
    return boundary


async def extract_document(
    *, ctx: ExtractContext, fields: Sequence[FieldSpec], client: ExtractorClient
) -> dict[str, object]:
    """Group `fields` into Retrieval Units and run ONE retrieval + ONE LLM
    call per unit, then restore the caller's field order. Signature unchanged
    -- `doc_key`/`module` are still attached by the route. Never raises."""
    boundary = await _consolidated_start_page(ctx)

    units = group_fields_by_unit(fields)
    flat: list[dict[str, object]] = []
    for unit in units:
        flat.extend(
            await extract_unit(
                ctx=ctx, unit=unit, client=client, consolidated_start_page=boundary,
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

    return {
        "schema_version": 1, "doc_id": ctx.doc_id,
        "field_count": len(results), "fields": results,
    }
