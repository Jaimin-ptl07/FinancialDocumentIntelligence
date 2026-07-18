"""OcrStage: the root pipeline stage that runs OCR over the intake'd PDF."""

import asyncio
from typing import cast

from fdi.core.ocr_schema import normalize_ocr
from fdi.pipeline.ocr import OcrClient
from fdi.pipeline.registry import REGISTRY
from fdi.pipeline.stage import StageContext


class OcrStage:
    name = "ocr"
    # BUMP when FDI_AZURE_DI_MODEL or api_version changes (they define the output), or when
    # normalize_ocr's schema changes. version=2: SP-7a enriched the artifact with the
    # structural payload (bounding_regions, spans, cells, sections) as schema_version 2.
    # version=3: SP-7b added table spans (offset/length) as schema_version 3.
    # version=4: SP-7d added figures (caption + bounding_regions) as schema_version 4.
    # version=5: Phase 0 retains cell elements + cell bounding_regions, table
    # caption/footnotes, page spans + selection_marks, and a low-confidence word list
    # -- the Azure structure Phases 1-5 consume -- as schema_version 5.
    version = 5
    deps: tuple[str, ...] = ()

    async def run(self, ctx: StageContext) -> object:
        pdf = await ctx.read_source()
        client = cast(OcrClient, ctx.services["ocr"])
        raw = await asyncio.to_thread(client.analyze, pdf)
        return normalize_ocr(raw)


REGISTRY.register(OcrStage())
