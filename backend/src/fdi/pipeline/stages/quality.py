"""QualityStage: a DERIVED stage that validates the AST for internal defects
and emits a quality report. Auto-chains off AstStage via `deps=("ast",)`."""

from collections.abc import Mapping
from typing import cast

from fdi.core.quality import AST_VALIDATORS, validate
from fdi.pipeline.registry import REGISTRY
from fdi.pipeline.stage import StageContext


class QualityStage:
    name = "quality"
    version = 1
    deps: tuple[str, ...] = ("ast",)

    async def run(self, ctx: StageContext) -> object:
        ast = cast(Mapping[str, object], await ctx.read("ast"))
        return validate(ast, AST_VALIDATORS)


REGISTRY.register(QualityStage())
