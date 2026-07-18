"""KnowledgeStage: a DERIVED stage that folds a document's semantic section
classification, ontology concept mappings, and quality report into ONE
unified per-document knowledge record via the pure `consolidate`. Auto-chains
off ALL THREE of SemanticStage, OntologyStage, and QualityStage via
deps=("semantic", "ontology", "quality") -- the codebase's first
THREE-dependency stage (SP-15)."""

from collections.abc import Mapping
from typing import cast

from fdi.core.knowledge import consolidate
from fdi.pipeline.registry import REGISTRY
from fdi.pipeline.stage import StageContext


class KnowledgeStage:
    name = "knowledge"
    version = 1
    deps: tuple[str, ...] = ("semantic", "ontology", "quality")

    async def run(self, ctx: StageContext) -> object:
        semantic = cast(Mapping[str, object], await ctx.read("semantic"))
        ontology = cast(Mapping[str, object], await ctx.read("ontology"))
        quality = cast(Mapping[str, object], await ctx.read("quality"))
        return consolidate(semantic, ontology, quality)


REGISTRY.register(KnowledgeStage())
