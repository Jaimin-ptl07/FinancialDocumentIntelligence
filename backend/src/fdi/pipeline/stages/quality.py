# backend/src/fdi/pipeline/stages/quality.py
"""QualityStage: a DERIVED stage that validates the AST for internal defects,
the structure artifact's tables for shape defects, AND the ontology
artifact's concept_values for reconciliation defects (BS balances, P&L
foots, CF ties), merging all three into ONE quality report via
`validate_all`. Auto-chains off AstStage, StructureStage, AND OntologyStage
via `deps=("ast", "structure", "ontology")` -- the pipeline's first
three-dependency stage before KnowledgeStage (Phase 4)."""

from collections.abc import Mapping
from typing import cast

from fdi.core.quality import (
    AST_VALIDATORS,
    RECONCILIATION_VALIDATORS,
    STRUCTURE_VALIDATORS,
    validate_all,
)
from fdi.pipeline.registry import REGISTRY
from fdi.pipeline.stage import StageContext


class QualityStage:
    name = "quality"
    version = 3
    deps: tuple[str, ...] = ("ast", "structure", "ontology")

    async def run(self, ctx: StageContext) -> object:
        ast = cast(Mapping[str, object], await ctx.read("ast"))
        structure = cast(Mapping[str, object], await ctx.read("structure"))
        ontology = cast(Mapping[str, object], await ctx.read("ontology"))
        return validate_all([
            (ast, AST_VALIDATORS),
            (structure, STRUCTURE_VALIDATORS),
            (ontology, RECONCILIATION_VALIDATORS),
        ])


REGISTRY.register(QualityStage())
