"""GraphStage: a DERIVED stage that builds the typed CONTAINS/READING_ORDER
graph from the naive AST. Auto-chains off AstStage via `deps=("ast",)`."""

from collections.abc import Mapping
from typing import cast

from fdi.core.graph import build_graph
from fdi.pipeline.registry import REGISTRY
from fdi.pipeline.stage import StageContext


class GraphStage:
    name = "graph"
    version = 1
    deps: tuple[str, ...] = ("ast",)

    async def run(self, ctx: StageContext) -> object:
        ast = await ctx.read("ast")
        return build_graph(cast(Mapping[str, object], ast))


REGISTRY.register(GraphStage())
