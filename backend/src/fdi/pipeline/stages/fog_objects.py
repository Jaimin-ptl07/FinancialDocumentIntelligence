"""FogObjectsStage: the deterministic Financial Object Graph.

Zero LLM calls -- every value here is parsed, not read by a model.

`ocr` IS A DECLARED DEP because this stage READS it. `pipeline/run.py` builds
`input_hash` from the DECLARED deps only, so an undeclared read sits outside the
cache key: an OCR re-run that corrects a cell value inside a note table changes
neither `note_index` (it holds no values) nor `statement_index` (page,
table_index, anchor score and footing verdict all unchanged), and the FOG would
be served stale. `structure` is depended on so a re-stitch invalidates the
graph."""

from collections.abc import Mapping
from typing import cast

from fdi.core.fog_build import build_fog
from fdi.pipeline.registry import REGISTRY
from fdi.pipeline.stage import StageContext


class FogObjectsStage:
    name = "fog_objects"
    # v3: a nil note row is a LineItem with `current=None` rather than the
    # block caption, which moves note `row_index` ordinals and therefore ids;
    # diagnostics gain `refs_beyond_bound` / `regions_without_statements`.
    # v2: LineItem bodies carry `source_row`, and a basis with several regions
    # no longer mints duplicate ids. The body shape is part of the cache key.
    version = 4
    deps: tuple[str, ...] = ("ocr", "statement_index", "note_index", "structure")

    async def run(self, ctx: StageContext) -> object:
        ocr = cast(Mapping[str, object], await ctx.read("ocr"))
        statement_index = cast(Mapping[str, object], await ctx.read("statement_index"))
        note_index = cast(Mapping[str, object], await ctx.read("note_index"))
        return build_fog(
            ocr, statement_index, note_index, doc_id=ctx.doc_id
        )


REGISTRY.register(FogObjectsStage())
