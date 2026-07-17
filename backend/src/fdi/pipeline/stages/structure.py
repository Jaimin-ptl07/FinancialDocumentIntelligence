"""StructureStage: a DERIVED stage that removes header/footer/page-number
paragraphs and stitches cross-page tables. Auto-chains off OcrStage via
`deps=("ocr",)`."""

from collections.abc import Mapping
from typing import cast

from fdi.core.structure import merge_cross_page_tables
from fdi.core.unified import Role, canonical_role
from fdi.pipeline.registry import REGISTRY
from fdi.pipeline.stage import StageContext

_DROP = {Role.HEADER, Role.FOOTER, Role.PAGE_NUMBER}


class StructureStage:
    name = "structure"
    version = 1
    deps: tuple[str, ...] = ("ocr",)

    async def run(self, ctx: StageContext) -> object:
        ocr = cast(Mapping[str, object], await ctx.read("ocr"))
        paragraphs = cast(list[Mapping[str, object]], ocr.get("paragraphs", []))
        tables = cast(list[Mapping[str, object]], ocr.get("tables", []))
        blocks = [
            {
                "role": canonical_role(cast("str | None", p.get("role"))).value,
                "text": cast(str, p.get("content", "")),
            }
            for p in paragraphs
            if canonical_role(cast("str | None", p.get("role"))) not in _DROP
        ]
        return {
            "schema_version": 1,
            "blocks": blocks,
            "tables": merge_cross_page_tables(tables, paragraphs),
        }


REGISTRY.register(StructureStage())
