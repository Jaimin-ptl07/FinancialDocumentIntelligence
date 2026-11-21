"""StatementIndexStage: promote face detection from a per-request helper to a
cached, inspectable artifact. Deps `(ocr, structure, basis)` -- `structure` is
depended on so a re-stitch invalidates the index even though the face is read
off `ocr` tables (stitching changes which tables exist downstream, and a stale
index would point at the wrong `table_index`)."""

from collections.abc import Mapping
from typing import cast

from fdi.core.fog_build import build_statement_index
from fdi.pipeline.registry import REGISTRY
from fdi.pipeline.stage import StageContext


class StatementIndexStage:
    name = "statement_index"
    # v3: bodies carry `regions` -- every scanned span, the raw basis span it
    # was narrowed from, and how many faces it yielded.
    # v2: bodies carry `discarded`, and a face is chosen once per
    # (basis, statement) across every region. `stage.version` is part of the
    # artifact key, so a shape change must bump it or a stale-shaped body is
    # served from cache.
    version = 4
    deps: tuple[str, ...] = ("ocr", "structure", "basis")

    async def run(self, ctx: StageContext) -> object:
        ocr = cast(Mapping[str, object], await ctx.read("ocr"))
        basis = cast(Mapping[str, object], await ctx.read("basis"))
        return build_statement_index(ocr, basis)


REGISTRY.register(StatementIndexStage())
