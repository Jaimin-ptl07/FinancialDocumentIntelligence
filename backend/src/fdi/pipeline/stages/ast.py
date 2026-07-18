"""AstStage: a DERIVED stage that builds the naive AST from the unified
document. Auto-chains off NormalizeStage via `deps=("normalize",)`.

# version=2: Phase 1 -- table-cell paragraphs (role=="table_cell") are
# excluded from section paragraphs, and non-first `title`-role blocks are now
# kept as sections (instead of silently discarded)."""

from collections.abc import Mapping
from typing import cast

from fdi.core.ast import build_ast
from fdi.pipeline.registry import REGISTRY
from fdi.pipeline.stage import StageContext


class AstStage:
    name = "ast"
    version = 2
    deps: tuple[str, ...] = ("normalize",)

    async def run(self, ctx: StageContext) -> object:
        unified = await ctx.read("normalize")
        return build_ast(cast(Mapping[str, object], unified))


REGISTRY.register(AstStage())
