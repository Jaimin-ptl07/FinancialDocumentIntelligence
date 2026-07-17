"""EmbeddingStage: a DERIVED stage that embeds one chunk per AST section via an
injected EmbeddingClient. Auto-chains off AstStage via `deps=("ast",)`.

Content-addressed on `derived_input_hash([ast.output_hash])` +
`EmbeddingStage.version` -- deliberate, since the embedding call is
non-deterministic: a second run against the same `ast` artifact is a store
cache hit that does NOT call the client again (proven in
tests/integration/test_embedding_stage.py with a call-counting fake)."""

from collections.abc import Mapping
from typing import cast

from fdi.pipeline.embeddings import EmbeddingClient
from fdi.pipeline.registry import REGISTRY
from fdi.pipeline.stage import StageContext


class EmbeddingStage:
    name = "embedding"
    version = 1
    deps: tuple[str, ...] = ("ast",)

    async def run(self, ctx: StageContext) -> object:
        ast = cast(Mapping[str, object], await ctx.read("ast"))
        client = cast(EmbeddingClient, ctx.services["embedder"])

        chunk_ids: list[str] = []
        headings: list[object] = []
        texts: list[str] = []
        sections_raw = ast.get("sections")
        if isinstance(sections_raw, list):
            for i, section in enumerate(sections_raw):
                if not isinstance(section, dict):
                    continue
                heading_raw = section.get("heading")
                heading = heading_raw if isinstance(heading_raw, str) else ""
                paragraphs_raw = section.get("paragraphs")
                paragraphs = (
                    [p for p in paragraphs_raw if isinstance(p, str)]
                    if isinstance(paragraphs_raw, list)
                    else []
                )
                chunk_ids.append(f"s{i}")
                headings.append(heading_raw)
                texts.append(heading + "\n" + "\n".join(paragraphs))

        vectors = client.embed(texts) if texts else []
        chunks = [
            {"chunk_id": cid, "heading": heading, "vector": vector}
            for cid, heading, vector in zip(chunk_ids, headings, vectors, strict=True)
        ]
        return {
            "schema_version": 1,
            "model": client.model,
            "dim": len(vectors[0]) if vectors else 0,
            "chunks": chunks,
        }


REGISTRY.register(EmbeddingStage())
