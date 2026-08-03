"""NoteIndexStage: cache the note index as an inspectable artifact.

Currently rebuilt in memory per extraction request. As an artifact it is
cached, diffable and auditable -- and a ref-grammar failure like RITES's 0/30
becomes visible in one file instead of silently degrading retrieval."""

from collections.abc import Mapping
from typing import cast

from fdi.core.fog_build import build_note_index
from fdi.pipeline.registry import REGISTRY
from fdi.pipeline.stage import StageContext


class NoteIndexStage:
    name = "note_index"
    version = 1
    deps: tuple[str, ...] = ("ocr", "structure", "basis")

    async def run(self, ctx: StageContext) -> object:
        ocr = cast(Mapping[str, object], await ctx.read("ocr"))
        basis = cast(Mapping[str, object], await ctx.read("basis"))
        return build_note_index(ocr, basis)


REGISTRY.register(NoteIndexStage())
