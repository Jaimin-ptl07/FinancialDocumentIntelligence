"""OcrStage: the root pipeline stage that runs OCR over the intake'd PDF."""

import asyncio
from typing import cast

from fdi.core.ocr_schema import normalize_ocr
from fdi.pipeline.ocr import OcrClient
from fdi.pipeline.registry import REGISTRY
from fdi.pipeline.stage import StageContext


class OcrStage:
    name = "ocr"
    version = 1  # BUMP when FDI_AZURE_DI_MODEL or api_version changes (they define the output)
    deps: tuple[str, ...] = ()

    async def run(self, ctx: StageContext) -> object:
        pdf = await ctx.read_source()
        client = cast(OcrClient, ctx.services["ocr"])
        raw = await asyncio.to_thread(client.analyze, pdf)
        return normalize_ocr(raw)


REGISTRY.register(OcrStage())
