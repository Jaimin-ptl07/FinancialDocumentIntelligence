"""IndexStage: a DERIVED stage that upserts SP-13's embedding artifact
(one chunk = one vector) into a VectorStore via an injected client. Auto-chains
off EmbeddingStage via `deps=("embedding",)`. The artifact is a small
deterministic receipt (upserted count) -- the vector-store write is the side
effect and the actual point; see design spec sec 2.9-10 for why a cache hit
does not re-run the upsert, and why zero chunks skips the call entirely
(unlike SP-9b's GraphLoadStage)."""

from collections.abc import Mapping
from typing import cast

from fdi.pipeline.registry import REGISTRY
from fdi.pipeline.stage import StageContext
from fdi.pipeline.vectorstore import VectorStore


class IndexStage:
    name = "index"
    version = 1
    deps: tuple[str, ...] = ("embedding",)

    async def run(self, ctx: StageContext) -> object:
        embedding = cast(Mapping[str, object], await ctx.read("embedding"))
        store = cast(VectorStore, ctx.services["vector_store"])

        chunks_raw = embedding.get("chunks")
        chunks = (
            [c for c in chunks_raw if isinstance(c, dict)] if isinstance(chunks_raw, list) else []
        )

        items: list[Mapping[str, object]] = [
            {
                "id": f"{ctx.doc_id}:{chunk['chunk_id']}",
                "vector": chunk["vector"],
                "metadata": {"heading": chunk.get("heading"), "chunk_id": chunk["chunk_id"]},
            }
            for chunk in chunks
        ]

        upserted = cast(int, store.upsert(items)["upserted"]) if items else 0
        return {"schema_version": 1, "upserted": upserted}


REGISTRY.register(IndexStage())
