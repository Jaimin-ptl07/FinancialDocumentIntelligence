"""The extractor's orchestrator + ExtractorClient seam (design spec sec
3.2): extract_field pools context per field (hybrid_search + get_tables for
numeric fields), reranks via fdi.core.extractor (pure), prompts the client,
and assembles the schema's output_contract record. extract_document loops
every field. ExtractorClient.extract(*, system, user) -> Mapping is
SYNCHRONOUS, mirroring fdi.pipeline.agent.AgentClient.complete exactly.
AzureOpenAIExtractorClient (fdi.platform.extractor, does I/O) structurally
satisfies this protocol without importing it -- platform may not import
pipeline. FakeExtractorClient lives here for tests: no live Azure OpenAI
call anywhere in the default suite.

Never raises (decision 10): extract_field guards every I/O step (hybrid_
search, get_tables, client.extract) individually, so one tool's failure
degrades this field's pool/answer, never the whole document's batch.

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
    is_numeric,
    rerank_candidates,
)
from fdi.pipeline.embeddings import EmbeddingClient
from fdi.pipeline.retrieval import hybrid_search
from fdi.pipeline.tables import get_tables
from fdi.pipeline.vectorstore import VectorStore
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


_POOL_K = 12
_TABLE_BASE_SCORE = 0.5


def _render_table(table: Mapping[str, object]) -> str:
    """A canonicalized table grid (fdi.pipeline.tables.get_tables' output
    shape) -> plain text an LLM can read: header row, then one line per
    data row, cells pipe-separated. Empty/malformed input -> "" (extract_
    field adds no candidate for an empty render)."""
    grid_body = table.get("grid")
    if not isinstance(grid_body, Mapping):
        return ""
    lines: list[str] = []
    headers = grid_body.get("column_headers")
    if isinstance(headers, list):
        lines.append(" | ".join(str(h) for h in headers))
    rows = grid_body.get("grid")
    if isinstance(rows, list):
        for row in rows:
            if isinstance(row, list):
                lines.append(" | ".join(str(c) for c in row))
    return "\n".join(lines)


async def extract_field(
    *, ctx: ExtractContext, field: FieldSpec, client: ExtractorClient
) -> dict[str, object]:
    """pool (hybrid_search, scoped to {ctx.doc_key, ctx.version}, +
    get_tables(ctx.doc_id) if numeric) -> Candidate list -> rerank_
    candidates -> build_extraction_messages -> client.extract ->
    assemble_result. Never raises."""
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

    if is_numeric(field.data_type):
        tables: list[dict[str, object]] | None = None
        with contextlib.suppress(Exception):  # never raise (decision 10)
            tables = await get_tables(session=ctx.session, store=ctx.store, doc_id=ctx.doc_id)
        for t in tables or []:
            text = _render_table(t)
            if text:
                candidates.append(Candidate(
                    evidence_id=f"table:{ctx.doc_id}:{t.get('table_index')}",
                    text=text, page=None, score=_TABLE_BASE_SCORE, source_tool="table",
                ))

    ranked = rerank_candidates(candidates, field.keywords)
    if not ranked:
        return assemble_result(field, {}, ranked)

    system, user = build_extraction_messages(field, ranked)
    raw: Mapping[str, object] = {}
    with contextlib.suppress(Exception):  # never raise (decision 10)
        raw = client.extract(system=system, user=user)
    return assemble_result(field, raw, ranked)


async def extract_document(
    *, ctx: ExtractContext, fields: Sequence[FieldSpec], client: ExtractorClient
) -> dict[str, object]:
    """Loop `fields`, calling extract_field for each; always returns one
    record per field, never raises (decision 10). Empty `fields` ->
    field_count 0, fields []. `doc_key`/`module` are NOT part of this
    return -- the route (which already holds both from the request body)
    attaches them; see fdi.api.routes.extract_document_route (Task 5)."""
    results = [await extract_field(ctx=ctx, field=field, client=client) for field in fields]
    return {
        "schema_version": 1, "doc_id": ctx.doc_id,
        "field_count": len(results), "fields": results,
    }
