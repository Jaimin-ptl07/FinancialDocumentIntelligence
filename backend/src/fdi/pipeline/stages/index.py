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
    version = 2
    deps: tuple[str, ...] = ("embedding", "chunks")

    async def run(self, ctx: StageContext) -> object:
        embedding = cast(Mapping[str, object], await ctx.read("embedding"))
        chunks_art = cast(Mapping[str, object], await ctx.read("chunks"))
        store = cast(VectorStore, ctx.services["vector_store"])

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
                    "heading": chunk.get("heading"),
                    "chunk_id": chunk["chunk_id"],
                    "section_type": meta.get("section_type") if meta is not None else None,
                    "page": meta.get("page") if meta is not None else None,
                },
            })

        upserted = cast(int, store.upsert(items)["upserted"]) if items else 0
        return {"schema_version": 1, "upserted": upserted}


REGISTRY.register(IndexStage())
