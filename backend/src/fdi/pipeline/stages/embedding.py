"""EmbeddingStage: a DERIVED stage that embeds the vector-channel subset of the
`chunks` artifact (RT-1) via an injected EmbeddingClient. Auto-chains off
ChunksStage via `deps=("chunks",)`.

Phase 3a (single source-of-truth refactor): chunk_id/heading/text are read
directly from the `chunks` artifact -- the same one ChunksStage/IndexStage/
resolve_chunk(s) already use -- instead of being re-derived independently from
`ast`. This means table-derived whole-table chunks (`t{i}`) are embedded for
free, with ZERO code change to IndexStage (its metadata join is already generic
over `chunk_id`) or fdi.pipeline.retrieval.resolve_chunk(s) (format-agnostic
string match on `chunk_id`).

Phase 7 (de-dilute the vector channel): fine-grained table-ROW chunks
(`t{i}r{j}`) are SKIPPED here via `fdi.core.retrieval.is_row_chunk_id` -- they
are near-duplicates in embedding space and drown out the section/whole-table
context a field actually needs. Row chunks remain in the `chunks` artifact
untouched, so keyword/FTS (`index_chunks_fts`) and `resolve_chunks` still see
them; they simply leave the vector index.

Content-addressed on `derived_input_hash([chunks.output_hash])` +
`EmbeddingStage.version` -- deliberate, since the embedding call is
non-deterministic: a second run against the same `chunks` artifact is a store
cache hit that does NOT call the client again (proven in
tests/integration/test_embedding_stage.py with a call-counting fake)."""

from collections.abc import Mapping
from typing import cast

from fdi.core.retrieval import is_row_chunk_id
from fdi.pipeline.embeddings import EmbeddingClient
from fdi.pipeline.registry import REGISTRY
from fdi.pipeline.stage import StageContext


class EmbeddingStage:
    name = "embedding"
    version = 3
    deps: tuple[str, ...] = ("chunks",)

    async def run(self, ctx: StageContext) -> object:
        chunks_art = cast(Mapping[str, object], await ctx.read("chunks"))
        client = cast(EmbeddingClient, ctx.services["embedder"])

        records_raw = chunks_art.get("chunks")
        records = [c for c in records_raw if isinstance(c, dict)] if isinstance(
            records_raw, list
        ) else []
        records = [r for r in records if not is_row_chunk_id(str(r.get("chunk_id", "")))]

        texts = [cast(str, r.get("text", "")) for r in records]
        vectors = client.embed(texts) if texts else []
        chunks = [
            {"chunk_id": r["chunk_id"], "heading": r.get("heading"), "vector": vector}
            for r, vector in zip(records, vectors, strict=True)
        ]
        return {
            "schema_version": 1,
            "model": client.model,
            "dim": len(vectors[0]) if vectors else 0,
            "chunks": chunks,
        }


REGISTRY.register(EmbeddingStage())
