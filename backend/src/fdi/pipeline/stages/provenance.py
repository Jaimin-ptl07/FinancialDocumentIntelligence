"""ProvenanceStage: a DERIVED stage that resolves every SP-9 graph node back
to its source OCR paragraph's page + bounding geometry. The codebase's FIRST
two-dependency stage: deps=("graph", "ocr").

# version=2: Phase 2 -- build_provenance resolves via a direct source_index
# lookup instead of label text matching (fixes the ambiguous-match bug); the
# `node_provenance` entry shape and schema_version(1) are unchanged, but every
# cached provenance artifact was potentially wrong under the old algorithm, so
# the stage version is bumped to force recompute."""

from collections.abc import Mapping
from typing import cast

from fdi.core.provenance import build_provenance
from fdi.pipeline.registry import REGISTRY
from fdi.pipeline.stage import StageContext


class ProvenanceStage:
    name = "provenance"
    version = 2
    deps: tuple[str, ...] = ("graph", "ocr")

    async def run(self, ctx: StageContext) -> object:
        graph = cast(Mapping[str, object], await ctx.read("graph"))
        ocr = cast(Mapping[str, object], await ctx.read("ocr"))
        return build_provenance(graph, ocr)


REGISTRY.register(ProvenanceStage())
