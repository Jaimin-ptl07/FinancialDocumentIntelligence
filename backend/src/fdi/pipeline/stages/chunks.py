"""ChunksStage: the RT-1 retrieval foundation. A DERIVED stage joining `ast`,
`semantic`, `provenance`, `structure`, and (TR-1 P2) `basis` into a per-document
`chunks` artifact -- one record per AST section (text + section_type + page)
under the shared `s{i}` identity, PLUS one table CARD (`t{i}`) per
canonicalized table in `structure`, tagged with its standalone/consolidated
`basis`. No injected service, no external call: a pure function of five
cached artifacts.

Pre-check finding: SemanticStage emits one `sections` entry per counted AST
section in AST order (see stages/semantic.py), so build_chunks' positional
section_type alignment holds; the None fallback covers any drift.

version=3: TR-1 P2 adds `basis` as a dep and replaces the Phase 3a per-row
`t{i}r{r}` chunks + capped whole-table blob with ONE table card per table
(`docs/superpowers/specs/2026-07-22-table-representation-design.md` sec 4,
subset scoped to build_chunks/ChunksStage -- grid rendering and basis-aware
retrieval filtering are deferred to P3/P4b).

version=4: L1 fix -- a table card's row labels are taken from the DESCRIPTIVE
column (chosen by text density) instead of grid column 0, so a statement/note
table whose leading column is a roman-numeral index no longer produces a
semantically-empty card that never retrieves
(`docs/superpowers/specs/2026-07-23-l1-card-label-column-design.md`).
"""

from collections.abc import Mapping
from typing import cast

from fdi.core.retrieval import build_chunks
from fdi.pipeline.registry import REGISTRY
from fdi.pipeline.stage import StageContext


class ChunksStage:
    name = "chunks"
    version = 4
    deps: tuple[str, ...] = ("ast", "semantic", "provenance", "structure", "basis")

    async def run(self, ctx: StageContext) -> object:
        ast = cast(Mapping[str, object], await ctx.read("ast"))
        semantic = cast(Mapping[str, object], await ctx.read("semantic"))
        provenance = cast(Mapping[str, object], await ctx.read("provenance"))
        structure = cast(Mapping[str, object], await ctx.read("structure"))
        basis = cast(Mapping[str, object], await ctx.read("basis"))
        return build_chunks(ast, semantic, provenance, structure, basis)


REGISTRY.register(ChunksStage())
