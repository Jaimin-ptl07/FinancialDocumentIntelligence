"""IndexStage: a DERIVED stage that upserts SP-13's embedding artifact
(one chunk = one vector) into a VectorStore via an injected client. Auto-chains
off EmbeddingStage AND (v2, RT-1) ChunksStage via `deps=("embedding","chunks")`.
The artifact is a small deterministic receipt (upserted count) -- the
vector-store write is the side effect and the actual point; see design spec
sec 2.9-10 for why a cache hit does not re-run the upsert, and why zero chunks
skips the call entirely (unlike SP-9b's GraphLoadStage).

RT-1 v2: metadata is enriched with `section_type`/`page` read from the `chunks`
artifact (keyed by `chunk_id`), so a vector search can filter by statement type
at the vector layer.

RT-9 v3: metadata now also carries `text` (the chunk's body text, read from
the `chunks` artifact) and `doc_id`. `text` is the BM25 input -- Milvus scores
it server-side against an analyzed field, so the row must carry the text or
BM25 has nothing to score. `doc_id` is written as a filterable scalar because
`StageContext` has no `doc_key`/`version` (a stage has no session, by design),
which is why the retrieval layer keeps its own Postgres translation from
`doc_id` to `doc_key`/`version`. The version bump to 3 is the re-index
mechanism: the DAG is content-addressed on `{stage}/v{version}`, so already-
ingested documents re-run `index` -- and only `index` -- to backfill `text`/
`doc_id`; `embedding` artifacts stay cached, so there is no re-embedding and no
extra Azure cost.

Pre-check finding: `MilvusVectorStore.upsert` (platform/vectorstore.py) spreads
`**metadata` directly into each row dict passed to `MilvusClient.upsert` --
there is no fixed collection-schema column list to extend. `section_type`
(str | None) and `page` (int | None) are scalar values of the same shape as the
existing `heading` field, so they are accepted without any store change.
`FakeVectorStore` already stores arbitrary metadata dicts."""

from collections.abc import Mapping
from typing import cast

from fdi.pipeline.registry import REGISTRY
from fdi.pipeline.stage import StageContext
from fdi.pipeline.vectorstore import VectorStore


class IndexStage:
    name = "index"
    version = 3
    deps: tuple[str, ...] = ("embedding", "chunks")

    async def run(self, ctx: StageContext) -> object:
        embedding = cast(Mapping[str, object], await ctx.read("embedding"))
        chunks_art = cast(Mapping[str, object], await ctx.read("chunks"))

        # Milvus is OPTIONAL (the `vector` Compose profile is opt-in and
        # FDI_MILVUS_URI defaults to None), so `vector_store` may be absent.
        # Same reasoning as GraphLoadStage: an unconfigured backend is a
        # reportable outcome, not a KeyError.
        raw = ctx.services.get("vector_store")
        if raw is None:
            return {
                "schema_version": 1, "upserted": 0,
                "skipped": "vector_store not configured",
            }
        store = cast(VectorStore, raw)

        chunks_raw = embedding.get("chunks")
        chunks = (
            [c for c in chunks_raw if isinstance(c, dict)] if isinstance(chunks_raw, list) else []
        )

        meta_raw = chunks_art.get("chunks")
        meta_by_chunk: dict[str, Mapping[str, object]] = {}
        for c in meta_raw if isinstance(meta_raw, list) else []:
            if isinstance(c, Mapping):
                cid = c.get("chunk_id")
                if isinstance(cid, str):
                    meta_by_chunk[cid] = c

        items: list[Mapping[str, object]] = []
        for chunk in chunks:
            meta = meta_by_chunk.get(str(chunk["chunk_id"]))
            items.append({
                "id": f"{ctx.doc_id}:{chunk['chunk_id']}",
                "vector": chunk["vector"],
                "metadata": {
                    # `text` is the BM25 input -- schema v2's whole point. A
                    # chunk with no matching `chunks` record still indexes,
                    # with empty text, rather than being dropped.
                    "text": (meta.get("text") if meta is not None else None) or "",
                    "doc_id": ctx.doc_id,
                    "chunk_id": chunk["chunk_id"],
                    "heading": chunk.get("heading"),
                    "section_type": meta.get("section_type") if meta is not None else None,
                    "page": meta.get("page") if meta is not None else None,
                },
            })

        upserted = cast(int, store.upsert(items)["upserted"]) if items else 0
        return {"schema_version": 1, "upserted": upserted}


REGISTRY.register(IndexStage())
