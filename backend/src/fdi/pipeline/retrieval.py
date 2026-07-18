"""Retrieval orchestrator helpers: turn a hit id `{doc_id}:{chunk_id}` (Milvus,
or later a graph node) into its full RT-1 chunk record (text + citation) by
reading the `chunks` artifact. Pipeline layer (does I/O) -- imports platform
(repo + store) + core, the same shape as resolve_entities. RT-2..RT-8 call
resolve_chunks to attach text to a whole result set in one read per document.

RT-2's vector_search (below) is the first consumer: embed the query, search the
injected VectorStore, filter by section_type (Milvus metadata, in-memory) and/or
doc_key/version (one batched Postgres join via DocumentRepository.get_many), then
delegate the filter/join/truncate assembly to fdi.core.retrieval.build_vector_results
(pure, independently unit-tested)."""

from collections.abc import Mapping
from typing import cast

from sqlalchemy import func, select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from fdi.core.retrieval import build_vector_results, chunk_rows_for_index, fuse
from fdi.pipeline.embeddings import EmbeddingClient
from fdi.pipeline.vectorstore import VectorStore
from fdi.platform.db.models import Chunk
from fdi.platform.db.repository import DocumentRepository, RegistryRepository
from fdi.platform.storage.artifact_store import ArtifactStore


async def _read_chunks(
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
    for c in await _read_chunks(repo, store, doc_id):
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
        records = {c.get("chunk_id"): c for c in await _read_chunks(repo, store, doc_id)}
        for chunk_id in chunk_ids:
            rec = records.get(chunk_id)
            if rec is not None:
                out[f"{doc_id}:{chunk_id}"] = {"doc_id": doc_id, **rec}
    return out


_OVERFETCH_FACTOR = 5


async def vector_search(
    *,
    session: AsyncSession,
    store: ArtifactStore,
    embedder: EmbeddingClient,
    vector_store: VectorStore,
    query: str,
    k: int,
    filters: Mapping[str, object] | None = None,
) -> list[dict[str, object]]:
    """RT-2's semantic retrieval orchestrator: embed `query`, search
    `vector_store`, filter/join/cite via build_vector_results, attaching text
    through resolve_chunks. Over-fetches k*_OVERFETCH_FACTOR from the vector
    store when any filter is set (VectorStore.search has no filter parameter --
    design spec sec 2.3-4), single-shot: no adaptive re-query on under-fill.
    Exactly one embed call, one vector_store.search call, one
    DocumentRepository.get_many call, one resolve_chunks call -- no N+1."""
    section_type = filters.get("section_type") if filters else None
    doc_key = filters.get("doc_key") if filters else None
    version = filters.get("version") if filters else None
    has_filter = section_type is not None or doc_key is not None or version is not None

    [vector] = embedder.embed([query])
    fetch_k = k * _OVERFETCH_FACTOR if has_filter else k
    hits = vector_store.search(vector, fetch_k)

    doc_ids = {str(h["id"]).split(":", 1)[0] for h in hits if ":" in str(h["id"])}
    doc_rows = await DocumentRepository(session).get_many(list(doc_ids))
    docs: dict[str, Mapping[str, object]] = {
        doc_id: {"doc_key": d.doc_key, "version": d.version} for doc_id, d in doc_rows.items()
    }

    resolved = await resolve_chunks(session=session, store=store, ids=[str(h["id"]) for h in hits])

    return build_vector_results(
        cast(list[Mapping[str, object]], hits),
        resolved,
        docs,
        section_type=cast(str | None, section_type),
        doc_key=cast(str | None, doc_key),
        version=cast(int | None, version),
        k=k,
    )


async def index_chunks_fts(*, session: AsyncSession, store: ArtifactStore, doc_id: str) -> int:
    """Upsert `doc_id`'s `chunks` artifact into the `chunk` table. Returns the
    number of rows upserted (0 if the document is unknown or has no `chunks`
    artifact yet -- degrades, never raises, mirroring resolve_chunk). Caller
    owns the transaction (commits), same convention as every function in this
    module and fdi.pipeline.crossdoc.resolve_entities."""
    repo = RegistryRepository(session)
    doc = await repo.get_document(doc_id)
    if doc is None:
        return 0
    chunks = await _read_chunks(repo, store, doc_id)
    rows = chunk_rows_for_index(
        doc_id=doc_id, doc_key=doc.doc_key, version=doc.version, chunks=chunks
    )
    if not rows:
        return 0

    stmt = pg_insert(Chunk).values(rows)
    stmt = stmt.on_conflict_do_update(
        index_elements=["id"],
        set_={
            "doc_key": stmt.excluded.doc_key,
            "version": stmt.excluded.version,
            "heading": stmt.excluded.heading,
            "text": stmt.excluded.text,
            "section_type": stmt.excluded.section_type,
            "page": stmt.excluded.page,
            "updated_at": func.now(),
        },
    )
    await session.execute(stmt)
    await session.flush()
    return len(rows)


async def keyword_search(
    *,
    session: AsyncSession,
    query: str,
    k: int = 5,
    section_type: str | None = None,
    doc_key: str | None = None,
    version: int | None = None,
) -> list[dict[str, object]]:
    """Postgres full-text search over `chunk`. plainto_tsquery (never raises
    on free text, unlike to_tsquery) + ts_rank, best-first, optionally
    filtered. Returns citation-shaped hits carrying their own text -- unlike
    a Milvus hit, no second resolve step is needed."""
    tsquery = func.plainto_tsquery("english", query)
    score = func.ts_rank(Chunk.tsv, tsquery).label("score")
    stmt = select(Chunk, score).where(Chunk.tsv.op("@@")(tsquery))
    if section_type is not None:
        stmt = stmt.where(Chunk.section_type == section_type)
    if doc_key is not None:
        stmt = stmt.where(Chunk.doc_key == doc_key)
    if version is not None:
        stmt = stmt.where(Chunk.version == version)
    stmt = stmt.order_by(score.desc()).limit(k)

    rows = (await session.execute(stmt)).all()
    return [
        {
            "doc_id": c.doc_id, "doc_key": c.doc_key, "version": c.version,
            "chunk_id": c.chunk_id, "heading": c.heading, "page": c.page,
            "section_type": c.section_type, "text": c.text, "score": float(s),
        }
        for c, s in rows
    ]


_FUSE_OVERFETCH_FACTOR = 3


async def hybrid_search(
    *,
    session: AsyncSession,
    store: ArtifactStore,
    embedder: EmbeddingClient | None,
    vector_store: VectorStore | None,
    query: str,
    k: int,
    filters: Mapping[str, object] | None = None,
) -> list[dict[str, object]]:
    """RT-4's hybrid retrieval orchestrator: fetch k*_FUSE_OVERFETCH_FACTOR
    from RT-3's keyword_search (always -- Postgres is mandatory config) and,
    if BOTH embedder and vector_store are configured, from RT-2's
    vector_search over the SAME query/filters; fuse via
    fdi.core.retrieval.fuse and truncate to k. Degrades to keyword-only
    (never raises, never needs a 503) when embedder/vector_store is None --
    design spec sec 2 decision 2. Pipeline layer: imports platform + core,
    no I/O left inline -- fusion itself lives in fdi.core.retrieval, pure
    and independently unit-tested."""
    fetch_k = k * _FUSE_OVERFETCH_FACTOR
    section_type = filters.get("section_type") if filters else None
    doc_key = filters.get("doc_key") if filters else None
    version = filters.get("version") if filters else None

    keyword_hits = await keyword_search(
        session=session,
        query=query,
        k=fetch_k,
        section_type=cast(str | None, section_type),
        doc_key=cast(str | None, doc_key),
        version=cast(int | None, version),
    )

    vector_hits: list[dict[str, object]] = []
    if embedder is not None and vector_store is not None:
        vector_hits = await vector_search(
            session=session,
            store=store,
            embedder=embedder,
            vector_store=vector_store,
            query=query,
            k=fetch_k,
            filters=filters,
        )

    return fuse(
        cast(list[Mapping[str, object]], vector_hits),
        cast(list[Mapping[str, object]], keyword_hits),
        k=k,
    )
