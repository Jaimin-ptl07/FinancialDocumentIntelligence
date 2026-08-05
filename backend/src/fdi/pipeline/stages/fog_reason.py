"""FogReasonStage: compose, validate, calibrate and cover -- pure and free.

Deps declare BOTH `fog_mapping` and `fog_objects` because this stage reads
both: `run.py` builds the artifact key from DECLARED deps only, so an
undeclared read sits outside the cache key and a change to it would be served
stale forever.
"""

from collections.abc import Mapping
from typing import cast

from fdi.core.fog_reason import reason
from fdi.pipeline.registry import REGISTRY
from fdi.pipeline.stage import StageContext


def build_reasoning(
    fog: Mapping[str, object],
    mapping: Mapping[str, object],
    schema: Mapping[str, object],
) -> dict[str, object]:
    """Reason over every basis the FOG carries. Total: never raises."""
    bases_raw = fog.get("bases") if isinstance(fog, Mapping) else None
    bases = sorted(bases_raw) if isinstance(bases_raw, Mapping) else []
    return {
        "schema_version": 4,
        "doc_id": str(fog.get("doc_id", "")) if isinstance(fog, Mapping) else "",
        "bases": {b: reason(fog, mapping, schema, basis=b) for b in bases},
    }


class FogReasonStage:
    name = "fog_reason"
    version = 4
    deps: tuple[str, ...] = ("fog_mapping", "fog_objects")

    async def run(self, ctx: StageContext) -> object:
        fog = cast(Mapping[str, object], await ctx.read("fog_objects"))
        mapping = cast(Mapping[str, object], await ctx.read("fog_mapping"))
        schema = cast(Mapping[str, object], ctx.services.get("schema") or {})
        return build_reasoning(fog, mapping, schema)


REGISTRY.register(FogReasonStage())
