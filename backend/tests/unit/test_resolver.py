"""The DAG resolver: topology + staleness, pure and metadata-only (no blob reads)."""

import pytest

from fdi.pipeline.keys import derived_input_hash
from fdi.pipeline.resolver import CycleError, DagResolver, RunFacts
from fdi.pipeline.stage import StageContext


class _S:
    def __init__(self, name: str, deps: tuple[str, ...] = (), version: int = 1) -> None:
        self.name, self.deps, self.version = name, deps, version

    async def run(self, ctx: StageContext) -> object:
        return {}


def _chain() -> dict[str, _S]:
    # a -> b -> c
    return {"a": _S("a"), "b": _S("b", ("a",)), "c": _S("c", ("b",))}


def test_topological_order_and_successors() -> None:
    r = DagResolver(_chain())
    assert r.topological_order() == ["a", "b", "c"]
    assert r.successors("a") == ["b"]
    assert r.successors("c") == []


def test_plan_is_from_stage_plus_transitive_successors() -> None:
    assert DagResolver(_chain()).plan("b") == ["b", "c"]


def test_unknown_dep_is_rejected() -> None:
    with pytest.raises(ValueError, match="unknown dep"):
        DagResolver({"b": _S("b", ("missing",))})


def test_cycle_is_rejected() -> None:
    with pytest.raises(CycleError):
        DagResolver({"a": _S("a", ("b",)), "b": _S("b", ("a",))})


def test_missing_record_is_stale() -> None:
    r = DagResolver(_chain())
    assert r.stale({}) == ["a", "b", "c"]


def test_version_bump_makes_that_stage_stale() -> None:
    stages = _chain()
    oh_a = "h_a"
    recorded = {
        "a": RunFacts(version=1, input_hash="ia", output_hash=oh_a),
        "b": RunFacts(version=1, input_hash=derived_input_hash([oh_a]), output_hash="h_b"),
        "c": RunFacts(version=1, input_hash=derived_input_hash(["h_b"]), output_hash="h_c"),
    }
    assert DagResolver(stages).stale(recorded) == []
    stages["b"].version = 2  # b improved
    assert DagResolver(stages).stale(recorded) == ["b"]


def test_regenerated_upstream_makes_downstream_stale() -> None:
    stages = _chain()
    recorded = {
        "a": RunFacts(version=1, input_hash="ia", output_hash="NEW_a"),  # a was regenerated
        "b": RunFacts(version=1, input_hash=derived_input_hash(["OLD_a"]), output_hash="h_b"),
        "c": RunFacts(version=1, input_hash=derived_input_hash(["h_b"]), output_hash="h_c"),
    }
    # b's recorded input_hash was computed from OLD_a; a now outputs NEW_a -> b stale.
    assert DagResolver(stages).stale(recorded) == ["b"]
