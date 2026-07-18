"""ChunksStage: the RT-1 retrieval foundation. A DERIVED stage joining `ast`,
`semantic`, `provenance`, and (Phase 3a) `structure` into a per-document `chunks`
artifact -- one record per AST section (text + section_type + page) under the
shared `s{i}` identity, PLUS one record per table data row (`t{i}r{j}`) and one
capped whole-table record (`t{i}`) per canonicalized table in `structure`. No
injected service, no external call: a pure function of four cached artifacts.

Pre-check finding: SemanticStage emits one `sections` entry per counted AST
section in AST order (see stages/semantic.py), so build_chunks' positional
section_type alignment holds; the None fallback covers any drift.

version=2: Phase 3a adds `structure` as a dep and table-derived chunks (SP
2026-07-19 ph3-table-chunks-design.md, subset scoped to build_chunks/ChunksStage/
EmbeddingStage -- table<->AST-section linking (`table_refs`) and the raw<->
structure table-index bridge are deferred to Phase 3b).
"""

from collections.abc import Mapping
from typing import cast

from fdi.core.retrieval import build_chunks
from fdi.pipeline.registry import REGISTRY
from fdi.pipeline.stage import StageContext


class ChunksStage:
    name = "chunks"
    version = 2
    deps: tuple[str, ...] = ("ast", "semantic", "provenance", "structure")

    async def run(self, ctx: StageContext) -> object:
        ast = cast(Mapping[str, object], await ctx.read("ast"))
        semantic = cast(Mapping[str, object], await ctx.read("semantic"))
        provenance = cast(Mapping[str, object], await ctx.read("provenance"))
        structure = cast(Mapping[str, object], await ctx.read("structure"))
        return build_chunks(ast, semantic, provenance, structure)


REGISTRY.register(ChunksStage())
