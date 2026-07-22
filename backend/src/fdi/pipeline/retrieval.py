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
injected VectorStore, filter by section_type (Milvus metadata, in-memory) and/or
doc_key/version (one batched Postgres join via DocumentRepository.get_many), then
delegate the filter/join/truncate assembly to fdi.core.retrieval.build_vector_results
(pure, independently unit-tested)."""

from collections.abc import Mapping, Sequence
from typing import cast

from sqlalchemy.ext.asyncio import AsyncSession

from fdi.core.retrieval import build_vector_results, fuse
from fdi.pipeline.embeddings import EmbeddingClient
from fdi.pipeline.vectorstore import VectorStore
from fdi.platform.db.repository import DocumentRepository, RegistryRepository
from fdi.platform.storage.artifact_store import ArtifactStore


async def read_chunks(
    repo: RegistryRepository, store: ArtifactStore, doc_id: str
) -> list[Mapping[str, object]]:
    run = await repo.latest_run(doc_id, "chunks")
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


_OVERFETCH_FACTOR = 5


def _milvus_filter(doc_ids: Sequence[str]) -> str:
    """Milvus filter expression scoping a search to specific documents.

    The collection stores `doc_id`, not `doc_key`/`version` -- a Stage has
    neither -- so a doc_key/version filter is resolved to doc_ids by the
    caller first. Empty string means unscoped."""
    if not doc_ids:
        return ""
    ids = ", ".join(f'"{d}"' for d in doc_ids)
    return f"doc_id in [{ids}]"


async def _docs_for(
    session: AsyncSession, doc_ids: list[str]
) -> dict[str, Mapping[str, object]]:
    rows = await DocumentRepository(session).get_many(doc_ids)
    return {
        doc_id: {"doc_key": d.doc_key, "version": d.version}
        for doc_id, d in rows.items()
    }


async def _doc_ids_for(
    session: AsyncSession, *, doc_key: str | None, version: int | None
) -> list[str]:
    """doc_key/version -> the doc_ids to scope a Milvus filter to. Returns []
    when no filter is requested (meaning unscoped, not 'match nothing')."""
    if doc_key is None and version is None:
        return []
    return await DocumentRepository(session).ids_for(doc_key=doc_key, version=version)


async def vector_search(
    *,
    session: AsyncSession,
    embedder: EmbeddingClient,
    vector_store: VectorStore,
    query: str,
    k: int,
    filters: Mapping[str, object] | None = None,
) -> list[dict[str, object]]:
    """RT-2's semantic retrieval orchestrator: embed `query`, search
    `vector_store`, filter/join/cite via build_vector_results. RT-9: the
    Milvus hit carries its own text, so there is no separate resolve_chunks
    call to attach it any more. Over-fetches k*_OVERFETCH_FACTOR from the
    vector store when any filter is set (VectorStore.search has no filter
    parameter -- design spec sec 2.3-4), single-shot: no adaptive re-query on
    under-fill. Exactly one embed call, one vector_store.search call, one
    DocumentRepository.get_many call -- no N+1."""
    section_type = filters.get("section_type") if filters else None
    doc_key = filters.get("doc_key") if filters else None
    version = filters.get("version") if filters else None

    doc_ids = await _doc_ids_for(
        session, doc_key=cast(str | None, doc_key), version=cast(int | None, version)
    )
    if (doc_key is not None or version is not None) and not doc_ids:
        return []

    fetch_k = k * _OVERFETCH_FACTOR if section_type is not None else k
    [vector] = embedder.embed([query])
    hits = vector_store.search(vector, fetch_k, filter=_milvus_filter(doc_ids))
    docs = await _docs_for(session, [str(h["id"]).split(":", 1)[0] for h in hits])

    return build_vector_results(
        cast(list[Mapping[str, object]], hits),
        docs,
        section_type=cast(str | None, section_type),
        doc_key=cast(str | None, doc_key),
        version=cast(int | None, version),
        k=k,
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

    Replaces RT-3's Postgres keyword_search, whose plainto_tsquery ANDed every
    lexeme and therefore returned zero hits for every real (sentence-shaped)
    query."""
    if not query.strip():
        return []
    section_type = filters.get("section_type") if filters else None
    doc_key = filters.get("doc_key") if filters else None
    version = filters.get("version") if filters else None

    doc_ids = await _doc_ids_for(
        session, doc_key=cast(str | None, doc_key), version=cast(int | None, version)
    )
    if (doc_key is not None or version is not None) and not doc_ids:
        return []

    fetch_k = k * _OVERFETCH_FACTOR if section_type is not None else k
    hits = vector_store.search_text(query.strip(), fetch_k, filter=_milvus_filter(doc_ids))
    docs = await _docs_for(session, [str(h["id"]).split(":", 1)[0] for h in hits])
    return build_vector_results(
        cast(list[Mapping[str, object]], hits), docs,
        section_type=cast(str | None, section_type),
        doc_key=cast(str | None, doc_key), version=cast(int | None, version), k=k,
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
    section_type = filters.get("section_type") if filters else None
    doc_key = filters.get("doc_key") if filters else None
    version = filters.get("version") if filters else None
    resolved_filters = {"section_type": section_type, "doc_key": doc_key, "version": version}

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
