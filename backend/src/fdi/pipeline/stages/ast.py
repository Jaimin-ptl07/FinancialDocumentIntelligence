"""AstStage: a DERIVED stage that builds the naive AST from the unified
document. Auto-chains off NormalizeStage via `deps=("normalize",)`.

# version=2: Phase 1 -- table-cell paragraphs (role=="table_cell") are
# excluded from section paragraphs, and non-first `title`-role blocks are now
# kept as sections (instead of silently discarded).
# version=3: Phase 2 -- carries `title_source_index`/`heading_source_index`/
# `paragraph_source_indices` through from each block's `source_index`.
# version=4: Phase 3b -- additively attaches `table_refs`/`figure_refs` to
# every section (`fdi.core.ast.attach_table_refs`), derived from the `ocr`
# artifact's `sections[].elements` hierarchy. New dep on `ocr`."""

from collections.abc import Mapping
from typing import cast

from fdi.core.ast import attach_table_refs, build_ast
from fdi.pipeline.registry import REGISTRY
from fdi.pipeline.stage import StageContext


class AstStage:
    name = "ast"
    version = 4
    deps: tuple[str, ...] = ("normalize", "ocr")

    async def run(self, ctx: StageContext) -> object:
        unified = await ctx.read("normalize")
        ocr = await ctx.read("ocr")
        ast = build_ast(cast(Mapping[str, object], unified))
        return attach_table_refs(ast, cast(Mapping[str, object], ocr))


REGISTRY.register(AstStage())
