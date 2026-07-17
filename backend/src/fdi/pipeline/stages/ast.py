"""AstStage: a DERIVED stage that builds the naive AST from the unified
document. Auto-chains off NormalizeStage via `deps=("normalize",)`."""

from collections.abc import Mapping
from typing import cast

from fdi.core.ast import build_ast
from fdi.pipeline.registry import REGISTRY
from fdi.pipeline.stage import StageContext


class AstStage:
    name = "ast"
    version = 1
    deps: tuple[str, ...] = ("normalize",)

    async def run(self, ctx: StageContext) -> object:
        unified = await ctx.read("normalize")
        return build_ast(cast(Mapping[str, object], unified))


REGISTRY.register(AstStage())
