"""BasisStage: a DERIVED stage that segments the document into standalone/
consolidated page regions. Auto-chains off AstStage + ProvenanceStage via
`deps=("ast", "provenance")`. Pure pass-through onto `fdi.core.basis.
detect_basis_regions` -- no successors yet (like `linking`), a later phase
(TR-1 P2/P4) tags table cards and filters retrieval with `page_basis`."""

from collections.abc import Mapping
from typing import cast

from fdi.core.basis import detect_basis_regions
from fdi.pipeline.registry import REGISTRY
from fdi.pipeline.stage import StageContext


class BasisStage:
    name = "basis"
    version = 1
    deps: tuple[str, ...] = ("ast", "provenance")

    async def run(self, ctx: StageContext) -> object:
        ast = cast(Mapping[str, object], await ctx.read("ast"))
        provenance = cast(Mapping[str, object], await ctx.read("provenance"))
        return detect_basis_regions(ast, provenance)


REGISTRY.register(BasisStage())
