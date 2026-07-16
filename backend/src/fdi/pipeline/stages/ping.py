"""The trivial root stage that proves the framework end to end."""

from fdi.pipeline.registry import REGISTRY
from fdi.pipeline.stage import StageContext


class PingStage:
    name = "ping"
    version = 1
    deps: tuple[str, ...] = ()

    async def run(self, ctx: StageContext) -> object:
        return {"kind": "ping", "schema_version": 1}


REGISTRY.register(PingStage())
