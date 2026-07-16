"""The name -> Stage map. Registration is where a duplicate name is caught; the DAG
shape (cycles, unknown deps) is validated later by the resolver over the whole map."""

import pytest

from fdi.pipeline.registry import StageRegistry
from fdi.pipeline.stage import StageContext


class _S:
    def __init__(self, name: str) -> None:
        self.name = name
        self.version = 1
        self.deps: tuple[str, ...] = ()

    async def run(self, ctx: StageContext) -> object:
        return {}


def test_register_and_get() -> None:
    r = StageRegistry()
    s = _S("a")
    r.register(s)
    assert r.get("a") is s
    assert r.all() == {"a": s}


def test_duplicate_name_is_rejected() -> None:
    r = StageRegistry()
    r.register(_S("a"))
    with pytest.raises(ValueError, match="already registered"):
        r.register(_S("a"))


def test_get_unknown_raises() -> None:
    with pytest.raises(KeyError):
        StageRegistry().get("nope")
