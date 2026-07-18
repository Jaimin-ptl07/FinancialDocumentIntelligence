"""NormalizeStage: a DERIVED stage that maps the OCR artifact into the unified
schema. Auto-chains off OcrStage via `deps=("ocr",)`.

# version=2: Phase 1 re-roles table-cell paragraphs (Role.TABLE_CELL, from
# `tables[].cells[].elements`) instead of their Azure-derived role, as
# schema_version 2. Blocks stay 1:1 with `ocr["paragraphs"]`."""

from collections.abc import Mapping
from typing import cast

from fdi.core.unified import normalize_document
from fdi.pipeline.registry import REGISTRY
from fdi.pipeline.stage import StageContext


class NormalizeStage:
    name = "normalize"
    version = 2
    deps: tuple[str, ...] = ("ocr",)

    async def run(self, ctx: StageContext) -> object:
        ocr = await ctx.read("ocr")
        return normalize_document(cast(Mapping[str, object], ocr))


REGISTRY.register(NormalizeStage())
