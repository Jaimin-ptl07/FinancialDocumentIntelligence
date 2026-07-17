"""ProvenanceStage: a DERIVED stage that resolves every SP-9 graph node back
to its source OCR paragraph's page + bounding geometry. The codebase's FIRST
two-dependency stage: deps=("graph", "ocr")."""

from collections.abc import Mapping
from typing import cast

from fdi.core.provenance import build_provenance
from fdi.pipeline.registry import REGISTRY
from fdi.pipeline.stage import StageContext


class ProvenanceStage:
    name = "provenance"
    version = 1
    deps: tuple[str, ...] = ("graph", "ocr")

    async def run(self, ctx: StageContext) -> object:
        graph = cast(Mapping[str, object], await ctx.read("graph"))
        ocr = cast(Mapping[str, object], await ctx.read("ocr"))
        return build_provenance(graph, ocr)


REGISTRY.register(ProvenanceStage())
