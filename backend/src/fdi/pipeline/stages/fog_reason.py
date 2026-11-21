"""FogReasonStage: compose, validate, calibrate and cover -- pure and free.

Deps declare BOTH `fog_mapping` and `fog_objects` because this stage reads
both: `run.py` builds the artifact key from DECLARED deps only, so an
undeclared read sits outside the cache key and a change to it would be served
stale forever.

The taxonomy schema is the THIRD such read, and deps cannot cover it. `run.py`
derives a stage's input_hash from its dependencies' OUTPUT hashes, so an
upstream artifact that is byte-identical leaves this stage's key untouched --
and a schema edit to a validation rule or the coverage taxonomy changes
nothing `fog_mapping` emits, because `fog_mapping` never reads either. Such an
edit would silently keep serving reasoning computed under the old rules. It is
declared in `key_material` for that reason.
"""

from collections.abc import Mapping
from typing import cast

from fdi.core.fog_reason import reason
from fdi.core.hashing import sha256_hex
from fdi.core.serialization import canonical_json_bytes
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

    def key_material(self, services: Mapping[str, object]) -> str:
        """The taxonomy schema's digest -- the one input this stage reads that
        its declared deps cannot represent. Canonical JSON so dict ordering
        cannot change the key. Identical to FogMappingStage's, deliberately:
        both stages read the same service and must invalidate on the same
        edit."""
        schema = services.get("schema")
        if not isinstance(schema, Mapping):
            return ""
        try:
            return sha256_hex(canonical_json_bytes(schema))
        except Exception:  # noqa: BLE001 - a key is never worth failing a run
            return ""


REGISTRY.register(FogReasonStage())
