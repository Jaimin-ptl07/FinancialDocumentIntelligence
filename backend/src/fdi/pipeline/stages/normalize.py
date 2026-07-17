"""NormalizeStage: a DERIVED stage that maps the OCR artifact into the unified
schema. Auto-chains off OcrStage via `deps=("ocr",)`."""

from collections.abc import Mapping
from typing import cast

from fdi.core.unified import normalize_document
from fdi.pipeline.registry import REGISTRY
from fdi.pipeline.stage import StageContext


class NormalizeStage:
    name = "normalize"
    version = 1
    deps: tuple[str, ...] = ("ocr",)

    async def run(self, ctx: StageContext) -> object:
        ocr = await ctx.read("ocr")
        return normalize_document(cast(Mapping[str, object], ocr))


REGISTRY.register(NormalizeStage())
