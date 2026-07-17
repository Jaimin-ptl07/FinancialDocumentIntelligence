# backend/src/fdi/pipeline/stages/quality.py
"""QualityStage: a DERIVED stage that validates the AST for internal defects
AND the structure artifact's tables for shape defects, merging both into ONE
quality report via `validate_all`. Auto-chains off BOTH AstStage and
StructureStage via `deps=("ast", "structure")` -- the pipeline's first
two-dependency stage (SP-17b)."""

from collections.abc import Mapping
from typing import cast

from fdi.core.quality import AST_VALIDATORS, STRUCTURE_VALIDATORS, validate_all
from fdi.pipeline.registry import REGISTRY
from fdi.pipeline.stage import StageContext


class QualityStage:
    name = "quality"
    version = 2
    deps: tuple[str, ...] = ("ast", "structure")

    async def run(self, ctx: StageContext) -> object:
        ast = cast(Mapping[str, object], await ctx.read("ast"))
        structure = cast(Mapping[str, object], await ctx.read("structure"))
        return validate_all([(ast, AST_VALIDATORS), (structure, STRUCTURE_VALIDATORS)])


REGISTRY.register(QualityStage())
