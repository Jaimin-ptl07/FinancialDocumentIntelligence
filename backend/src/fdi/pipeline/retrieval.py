"""Retrieval orchestrator helpers: turn a hit id `{doc_id}:{chunk_id}` (Milvus,
or later a graph node) into its full RT-1 chunk record (text + citation) by
reading the `chunks` artifact. Pipeline layer (does I/O) -- imports platform
(repo + store) + core, the same shape as resolve_entities. RT-2..RT-8 call
resolve_chunks to attach text to a whole result set in one read per document."""

from collections.abc import Mapping
from typing import cast

from sqlalchemy import func
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from fdi.core.retrieval import chunk_rows_for_index
from fdi.platform.db.models import Chunk
from fdi.platform.db.repository import RegistryRepository
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
