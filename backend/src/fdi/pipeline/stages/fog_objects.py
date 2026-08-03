"""FogObjectsStage: the deterministic Financial Object Graph.

Zero LLM calls -- every value here is parsed, not read by a model. Deps
`(statement_index, note_index, structure)`; `ocr` is reached through the
indices' own dependency chain, and `structure` is depended on so a re-stitch
invalidates the graph."""

from collections.abc import Mapping
from typing import cast

from fdi.core.fog_build import build_fog
from fdi.pipeline.registry import REGISTRY
from fdi.pipeline.stage import StageContext


class FogObjectsStage:
    name = "fog_objects"
    version = 1
    deps: tuple[str, ...] = ("statement_index", "note_index", "structure")

    async def run(self, ctx: StageContext) -> object:
        ocr = cast(Mapping[str, object], await ctx.read("ocr"))
        statement_index = cast(Mapping[str, object], await ctx.read("statement_index"))
        note_index = cast(Mapping[str, object], await ctx.read("note_index"))
        return build_fog(
            ocr, statement_index, note_index, doc_id=ctx.doc_id
        )


REGISTRY.register(FogObjectsStage())
