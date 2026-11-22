"""IssuerStage: a DERIVED stage that identifies which Company issued the
Report, by its CIN. Auto-chains off AstStage + ProvenanceStage via
`deps=("ast", "provenance")`. Pure pass-through onto `fdi.core.issuer.
identify_issuer`; stages have no Postgres access, so filing the Report under
its Company is `fdi.pipeline.companies.CompanyRegistry.resolve_report`'s job,
run by the worker once this stage completes."""

from collections.abc import Mapping
from typing import cast

from fdi.core.issuer import identify_issuer
from fdi.pipeline.registry import REGISTRY
from fdi.pipeline.stage import StageContext

ISSUER_STAGE = "issuer"


class IssuerStage:
    name = ISSUER_STAGE
    version = 1
    deps: tuple[str, ...] = ("ast", "provenance")

    async def run(self, ctx: StageContext) -> object:
        ast = cast(Mapping[str, object], await ctx.read("ast"))
        provenance = cast(Mapping[str, object], await ctx.read("provenance"))
        return identify_issuer(ast, provenance)


REGISTRY.register(IssuerStage())
