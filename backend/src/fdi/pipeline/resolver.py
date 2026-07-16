"""Pure DAG topology + staleness. No I/O: staleness is decided from recorded facts,
not by reading artifact blobs."""

from graphlib import CycleError as _GraphlibCycleError
from graphlib import TopologicalSorter

from fdi.core.facts import RunFacts as RunFacts
from fdi.pipeline.keys import derived_input_hash
from fdi.pipeline.stage import Stage


class CycleError(ValueError):
    """The registered stages form a dependency cycle."""


class DagResolver:
    def __init__(self, stages: dict[str, Stage]) -> None:
        for name, stage in stages.items():
            for dep in stage.deps:
                if dep not in stages:
                    raise ValueError(f"stage {name!r} has unknown dep {dep!r}")
        self._stages = stages
        # graphlib wants {node: predecessors}; deps ARE predecessors.
        sorter_graph: dict[str, set[str]] = {n: set(s.deps) for n, s in stages.items()}
        try:
            self._order: list[str] = list(TopologicalSorter(sorter_graph).static_order())
        except _GraphlibCycleError as exc:
            raise CycleError("stage dependency cycle") from exc

    def topological_order(self) -> list[str]:
        # static_order already respects deps; make ties deterministic by name within levels
        return sorted(self._order, key=lambda n: (self._depth(n), n))

    def _depth(self, name: str) -> int:
        deps = self._stages[name].deps
        return 0 if not deps else 1 + max(self._depth(d) for d in deps)

    def successors(self, name: str) -> list[str]:
        return sorted(n for n, s in self._stages.items() if name in s.deps)

    def plan(self, from_stage: str) -> list[str]:
        keep: set[str] = set()
        frontier = [from_stage]
        while frontier:
            cur = frontier.pop()
            if cur in keep:
                continue
            keep.add(cur)
            frontier.extend(self.successors(cur))
        return [n for n in self.topological_order() if n in keep]

    def stale(self, recorded: dict[str, RunFacts]) -> list[str]:
        out: list[str] = []
        for name in self.topological_order():
            stage = self._stages[name]
            rec = recorded.get(name)
            if rec is None or rec.version != stage.version:
                out.append(name)
                continue
            if stage.deps:
                expected = derived_input_hash(
                    [recorded[d].output_hash for d in stage.deps]
                    if all(d in recorded for d in stage.deps)
                    else ["<missing>"]
                )
                if rec.input_hash != expected:
                    out.append(name)
        return out
