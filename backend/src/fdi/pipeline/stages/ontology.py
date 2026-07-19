"""OntologyStage: a DERIVED stage that maps the AST's section headings/
paragraphs onto CanonicalConcepts (map_document) AND the structure artifact's
table grids onto CanonicalConcept numeric values (map_concept_values),
merging both into ONE artifact. Auto-chains off AstStage AND StructureStage
via deps=("ast", "structure") -- Phase 4."""

from collections.abc import Mapping
from typing import cast

from fdi.core.concept_values import map_concept_values
from fdi.core.ontology import map_document
from fdi.pipeline.registry import REGISTRY
from fdi.pipeline.stage import StageContext


class OntologyStage:
    name = "ontology"
    version = 2
    deps: tuple[str, ...] = ("ast", "structure")

    async def run(self, ctx: StageContext) -> object:
        ast = cast(Mapping[str, object], await ctx.read("ast"))
        structure = cast(Mapping[str, object], await ctx.read("structure"))
        doc_mappings = map_document(ast)
        values = map_concept_values(structure)
        return {
            "schema_version": 2,
            "mappings": doc_mappings["mappings"],
            "concept_values": values["concept_values"],
        }


REGISTRY.register(OntologyStage())
