"""The extractor's orchestrator + ExtractorClient seam (design spec sec
3.2): extract_field pools context per field via hybrid_search ALONE (dense
BGE + keyword Postgres-FTS, RRF-fused -- table content already reaches this
pool as chunks, since build_chunks emits t{i}/t{i}r{j} table chunks
alongside s{i} section chunks and EmbeddingStage embeds all of them; a
separate get_tables table-blob injection was measured to flood the rerank
with same-scored whole-table renders and HURT top-20 recall, 78.8% ->
88.1% once removed -- see docs/architecture/2026-07-19-ingestion-audit-
and-improvement-plan.md), reranks via fdi.core.extractor (pure), prompts
the client, and assembles the schema's output_contract record.
extract_document loops every field. ExtractorClient.extract(*, system,
user) -> Mapping is SYNCHRONOUS, mirroring fdi.pipeline.agent.AgentClient.
complete exactly. AzureOpenAIExtractorClient (fdi.platform.extractor, does
I/O) structurally satisfies this protocol without importing it -- platform
may not import pipeline. FakeExtractorClient lives here for tests: no live
Azure OpenAI call anywhere in the default suite.

Never raises (decision 10): extract_field guards every I/O step (hybrid_
search, client.extract) individually, so one tool's failure degrades this
field's pool/answer, never the whole document's batch.

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

from sqlalchemy.ext.asyncio import AsyncSession

from fdi.core.extractor import (
    Candidate,
    FieldSpec,
    assemble_result,
    build_extraction_messages,
    rerank_candidates,
)
from fdi.core.provenance import resolve_evidence_regions
from fdi.pipeline.embeddings import EmbeddingClient
from fdi.pipeline.retrieval import hybrid_search
from fdi.pipeline.vectorstore import VectorStore
from fdi.platform.db.repository import RegistryRepository
from fdi.platform.storage.artifact_store import ArtifactStore


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


async def extract_field(
    *, ctx: ExtractContext, field: FieldSpec, client: ExtractorClient
) -> dict[str, object]:
    """pool (hybrid_search ALONE, scoped to {ctx.doc_key, ctx.version}) ->
    Candidate list -> rerank_candidates -> build_extraction_messages ->
    client.extract -> assemble_result. Never raises."""
    candidates: list[Candidate] = []

    query = field.retrieval_query.strip()
    if query:
        hits: list[dict[str, object]] = []
        with contextlib.suppress(Exception):  # never raise (decision 10)
            hits = await hybrid_search(
                session=ctx.session, store=ctx.store, embedder=ctx.embedder,
                vector_store=ctx.vector_store, query=query, k=_POOL_K,
                filters={"doc_key": ctx.doc_key, "version": ctx.version},
            )
        for h in hits:
            candidates.append(Candidate(
                evidence_id=f"{h['doc_id']}:{h['chunk_id']}",
                text=str(h.get("text", "")),
                page=cast(int | None, h.get("page")),
                score=float(cast(float, h.get("score", 0.0))),
                source_tool="hybrid",
            ))

    ranked = rerank_candidates(candidates, field.keywords, top_k=_EXTRACT_TOP_K)
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


async def extract_document(
    *, ctx: ExtractContext, fields: Sequence[FieldSpec], client: ExtractorClient
) -> dict[str, object]:
    """Loop `fields`, calling extract_field for each; always returns one
    record per field, never raises (decision 10). Empty `fields` ->
    field_count 0, fields []. `doc_key`/`module` are NOT part of this
    return -- the route (which already holds both from the request body)
    attaches them; see fdi.api.routes.extract_document_route (Task 5).

    Each record's `source` additionally carries `bounding_regions`: the
    geometry behind its cited chunks, joined by the pure
    fdi.core.provenance.resolve_evidence_regions. The two artifacts it needs
    are read ONCE here, not per field -- the committed schema has 198 fields,
    so a per-field read would be 396 MinIO round trips for one request."""
    provenance = await _read_latest_artifact(ctx, "provenance")
    structure = await _read_latest_artifact(ctx, "structure")

    results = [
        _attach_regions(
            await extract_field(ctx=ctx, field=field, client=client),
            doc_id=ctx.doc_id, provenance=provenance, structure=structure,
        )
        for field in fields
    ]
    return {
        "schema_version": 1, "doc_id": ctx.doc_id,
        "field_count": len(results), "fields": results,
    }
