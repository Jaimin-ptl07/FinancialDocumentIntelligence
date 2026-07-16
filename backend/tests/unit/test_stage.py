"""The Stage contract and its context. A stage reads inputs through ctx and returns
a body; it never touches keys, hashing, storage, or the DB — the framework owns those."""

from fdi.pipeline.stage import Stage, StageContext


class _Echo:
    name = "echo"
    version = 1
    deps = ("upstream",)

    async def run(self, ctx: StageContext) -> object:
        return {"seen": await ctx.read("upstream")}


def test_stage_protocol_is_satisfied_structurally() -> None:
    stage: Stage = _Echo()  # must type-check + hold at runtime
    assert stage.name == "echo" and stage.version == 1 and stage.deps == ("upstream",)


async def test_context_read_delegates_to_its_reader() -> None:
    async def fake_reader(doc_id: str, stage_name: str) -> object:
        assert doc_id == "doc1" and stage_name == "upstream"
        return {"body": 42}

    ctx = StageContext(doc_id="doc1", read_artifact=fake_reader)
    assert await ctx.read("upstream") == {"body": 42}
    assert await _Echo().run(ctx) == {"seen": {"body": 42}}
