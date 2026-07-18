"""ChunksStage: the RT-1 retrieval foundation. A DERIVED stage joining `ast`,
`semantic`, and `provenance` into a per-document `chunks` artifact -- one record
per AST section (text + section_type + page) under the shared `s{i}` identity.
No injected service, no external call: a pure function of three cached artifacts.

Pre-check finding: SemanticStage emits one `sections` entry per counted AST
section in AST order (see stages/semantic.py), so build_chunks' positional
section_type alignment holds; the None fallback covers any drift.
"""

from collections.abc import Mapping
from typing import cast

from fdi.core.retrieval import build_chunks
from fdi.pipeline.registry import REGISTRY
from fdi.pipeline.stage import StageContext


class ChunksStage:
    name = "chunks"
    version = 1
    deps: tuple[str, ...] = ("ast", "semantic", "provenance")

    async def run(self, ctx: StageContext) -> object:
        ast = cast(Mapping[str, object], await ctx.read("ast"))
        semantic = cast(Mapping[str, object], await ctx.read("semantic"))
        provenance = cast(Mapping[str, object], await ctx.read("provenance"))
        return build_chunks(ast, semantic, provenance)


REGISTRY.register(ChunksStage())
