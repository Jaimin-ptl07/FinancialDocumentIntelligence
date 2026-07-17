"""LinkingStage: a DERIVED stage that surfaces figures (with captions) and footnotes
as structured elements. Auto-chains off OcrStage via `deps=("ocr",)`."""

from collections.abc import Mapping
from typing import cast

from fdi.core.links import extract_links
from fdi.pipeline.registry import REGISTRY
from fdi.pipeline.stage import StageContext


class LinkingStage:
    name = "linking"
    version = 1
    deps: tuple[str, ...] = ("ocr",)

    async def run(self, ctx: StageContext) -> object:
        ocr = cast(Mapping[str, object], await ctx.read("ocr"))
        return extract_links(ocr)


REGISTRY.register(LinkingStage())
