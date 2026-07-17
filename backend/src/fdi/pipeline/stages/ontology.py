"""OntologyStage: a DERIVED stage that maps the AST's section headings and
paragraphs onto CanonicalConcepts via the pure map_document. Auto-chains off
AstStage via `deps=("ast",)`."""

from collections.abc import Mapping
from typing import cast

from fdi.core.ontology import map_document
from fdi.pipeline.registry import REGISTRY
from fdi.pipeline.stage import StageContext


class OntologyStage:
    name = "ontology"
    version = 1
    deps: tuple[str, ...] = ("ast",)

    async def run(self, ctx: StageContext) -> object:
        ast = cast(Mapping[str, object], await ctx.read("ast"))
        return map_document(ast)


REGISTRY.register(OntologyStage())
