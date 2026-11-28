"""Retrieval orchestrator helpers: turn a hit id `{doc_id}:{chunk_id}` (Milvus,
or later a graph node) into its full RT-1 chunk record (text + citation) by
reading the `chunks` artifact. Pipeline layer (does I/O) -- imports platform
(repo + store) + core, the same shape as resolve_entities. resolve_chunk(s)
still attach text to a chunk read straight from the `chunks` artifact -- the
one remaining caller is RT-1's own citation lookup, not the search channels
below.

RT-9's bm25_search is the keyword channel: it passes `query` to
`VectorStore.search_text` as RAW TEXT -- Milvus tokenizes and scores it
server-side (BM25 Function over the analyzed `text` field) -- so, like
vector_search, its hits already carry their own text and citation metadata
and need no resolve_chunks call to attach it. This replaces RT-3's Postgres
keyword_search (plainto_tsquery + ts_rank), which ANDed every lexeme and
therefore returned zero hits for any real, sentence-shaped query.

RT-2's vector_search (below) is the other consumer: embed the query, search the
injected VectorStore with every filter applied inside the store, join the
doc_key/version citation (one batched Postgres read), and delegate the
join/truncate assembly to fdi.core.retrieval.build_vector_results.

S4: these channels are the Retrieval service's hybrid path
(fdi.pipeline.retrieval_service). Filters now include explicit `doc_ids`,
`bases` and `kinds`, and ALL of them -- section type included -- are applied
by the store, built by fdi.core.evidence.scope_filter_expression from resolved
ids and closed vocabularies only."""

from collections.abc import Mapping
from typing import cast

from sqlalchemy.ext.asyncio import AsyncSession

from fdi.core.evidence import DOC_ID_RE, Basis, EvidenceKind, scope_filter_expression
from fdi.core.retrieval import build_vector_results, fuse
from fdi.core.semantic import SectionType
from fdi.pipeline.embeddings import EmbeddingClient
from fdi.pipeline.vectorstore import VectorStore
from fdi.platform.db.repository import DocumentRepository, RegistryRepository
from fdi.platform.storage.artifact_store import ArtifactStore


async def read_chunks(
    repo: RegistryRepository, store: ArtifactStore, doc_id: str
) -> list[Mapping[str, object]]:
    run = await repo.latest_completed_run(doc_id, "chunks")
    if run is None or run.artifact_key is None:
        return []
    body = cast(Mapping[str, object], await store.get_json(run.artifact_key))
    chunks = body.get("chunks")
    return [c for c in chunks if isinstance(c, Mapping)] if isinstance(chunks, list) else []


async def resolve_chunk(
    *, session: AsyncSession, store: ArtifactStore, doc_id: str, chunk_id: str
) -> dict[str, object] | None:
    repo = RegistryRepository(session)
    for c in await read_chunks(repo, store, doc_id):
        if c.get("chunk_id") == chunk_id:
            return {"doc_id": doc_id, **c}
    return None


async def resolve_chunks(
    *, session: AsyncSession, store: ArtifactStore, ids: list[str]
) -> dict[str, dict[str, object]]:
    by_doc: dict[str, list[str]] = {}
    for full_id in ids:
        if ":" not in full_id:
            continue
        doc_id, chunk_id = full_id.split(":", 1)
        by_doc.setdefault(doc_id, []).append(chunk_id)

    repo = RegistryRepository(session)
    out: dict[str, dict[str, object]] = {}
    for doc_id, chunk_ids in by_doc.items():
        records = {c.get("chunk_id"): c for c in await read_chunks(repo, store, doc_id)}
        for chunk_id in chunk_ids:
            rec = records.get(chunk_id)
            if rec is not None:
                out[f"{doc_id}:{chunk_id}"] = {"doc_id": doc_id, **rec}
    return out


def _strs(value: object) -> tuple[str, ...]:
    if isinstance(value, str):
        return (value,)
    if isinstance(value, list | tuple):
        return tuple(v for v in value if isinstance(v, str))
    return ()


_SECTION_TYPES = frozenset(t.value for t in SectionType)
_BASES = frozenset(b.value for b in Basis)
_KINDS = frozenset(k.value for k in EvidenceKind)


async def _scope_filter(
    session: AsyncSession, filters: Mapping[str, object] | None
) -> str | None:
    """The vector-store filter expression for `filters`, or None when the
    filters can match NOTHING (an unknown doc_key, an out-of-vocabulary
    section type) -- which the channels answer with [] rather than widening
    to an unscoped search.

    Every filter is applied INSIDE the store (S4): `doc_key`/`version` and
    explicit `doc_ids` resolve to document ids in Postgres; `section_type`,
    `bases` and `kinds` must be closed-vocabulary values. Nothing is
    over-fetched and filtered in memory any more, so a filtered search
    returns k results whenever the store holds k matching rows."""
    f = filters or {}
    doc_key = f.get("doc_key")
    version = f.get("version")
    doc_ids: list[str] = []
    if doc_key is not None or version is not None:
        doc_ids = await DocumentRepository(session).ids_for(
            doc_key=cast(str | None, doc_key), version=cast(int | None, version)
        )
        if not doc_ids:
            return None
    explicit = f.get("doc_ids")
    if explicit is not None:
        wanted = [d for d in _strs(explicit) if DOC_ID_RE.match(d)]
        doc_ids = [d for d in doc_ids if d in wanted] if doc_ids else wanted
        if not doc_ids:
            return None

    section_types = _strs(f.get("section_type")) + _strs(f.get("section_types"))
    bases = _strs(f.get("bases"))
    kinds = _strs(f.get("kinds"))
    if any(t not in _SECTION_TYPES for t in section_types):
        return None
    if any(b not in _BASES for b in bases) or any(k not in _KINDS for k in kinds):
        return None
    return scope_filter_expression(
        doc_ids=doc_ids, bases=bases, kinds=kinds, section_types=section_types
    )


async def _docs_for(
    session: AsyncSession, doc_ids: list[str]
) -> dict[str, Mapping[str, object]]:
    rows = await DocumentRepository(session).get_many(doc_ids)
    return {
        doc_id: {"doc_key": d.doc_key, "version": d.version}
        for doc_id, d in rows.items()
    }


async def vector_search(
    *,
    session: AsyncSession,
    embedder: EmbeddingClient,
    vector_store: VectorStore,
    query: str,
    k: int,
    filters: Mapping[str, object] | None = None,
) -> list[dict[str, object]]:
    """RT-2's semantic channel: embed `query`, search `vector_store` with
    every filter applied in the store (`_scope_filter`), then join the
    doc_key/version citation via build_vector_results. Exactly one embed
    call, one vector_store.search call, one DocumentRepository.get_many call
    -- no N+1."""
    expression = await _scope_filter(session, filters)
    if expression is None:
        return []
    [vector] = embedder.embed([query])
    hits = vector_store.search(vector, k, filter=expression)
    docs = await _docs_for(session, [str(h["id"]).split(":", 1)[0] for h in hits])
    return build_vector_results(
        cast(list[Mapping[str, object]], hits), docs,
        section_type=None, doc_key=None, version=None, k=k,
    )


async def bm25_search(
    *,
    session: AsyncSession,
    vector_store: VectorStore,
    query: str,
    k: int,
    filters: Mapping[str, object] | None = None,
) -> list[dict[str, object]]:
    """RT-9's keyword channel: Milvus-native BM25 over the analyzed `text`
    field. `query` is passed as RAW TEXT -- Milvus tokenizes and scores it
    server-side; there is no client-side tokenization and no embedding call.
    Filters as `vector_search`."""
    if not query.strip():
        return []
    expression = await _scope_filter(session, filters)
    if expression is None:
        return []
    hits = vector_store.search_text(query.strip(), k, filter=expression)
    docs = await _docs_for(session, [str(h["id"]).split(":", 1)[0] for h in hits])
    return build_vector_results(
        cast(list[Mapping[str, object]], hits), docs,
        section_type=None, doc_key=None, version=None, k=k,
    )


_FUSE_OVERFETCH_FACTOR = 3


async def hybrid_search(
    *,
    session: AsyncSession,
    embedder: EmbeddingClient,
    vector_store: VectorStore,
    query: str,
    k: int,
    filters: Mapping[str, object] | None = None,
) -> list[dict[str, object]]:
    """RT-4's hybrid retrieval orchestrator: fetch k*_FUSE_OVERFETCH_FACTOR
    from RT-9's bm25_search and from RT-2's vector_search, both over the SAME
    query/filters; fuse via fdi.core.retrieval.fuse and truncate to k.
    `embedder`/`vector_store` are required here -- unlike the old
    keyword-mandatory/vector-optional split, both retrieval channels now live
    behind the same VectorStore seam, so there is nothing to degrade to; a
    caller with no configured vector_store has no channel to call at all
    (its 503 branch lives in fdi/api/routes.py, not here). Pipeline layer:
    imports platform + core, no I/O left inline -- fusion itself lives in
    fdi.core.retrieval, pure and independently unit-tested."""
    fetch_k = k * _FUSE_OVERFETCH_FACTOR
    resolved_filters = dict(filters or {})

    keyword_hits = await bm25_search(
        session=session,
        vector_store=vector_store,
        query=query,
        k=fetch_k,
        filters=resolved_filters,
    )

    vector_hits = await vector_search(
        session=session,
        embedder=embedder,
        vector_store=vector_store,
        query=query,
        k=fetch_k,
        filters=resolved_filters,
    )

    return fuse(
        cast(list[Mapping[str, object]], vector_hits),
        cast(list[Mapping[str, object]], keyword_hits),
        k=k,
    )
