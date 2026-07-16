from fdi.pipeline.stage import StageContext
from fdi.pipeline.stages.ping import PingStage


async def test_ping_body_is_deterministic() -> None:
    async def _no_read(d: str, s: str) -> object:  # ping is a root stage; never called
        raise AssertionError("ping must not read upstream")

    ctx = StageContext(doc_id="d", read_artifact=_no_read)
    assert await PingStage().run(ctx) == {"kind": "ping", "schema_version": 1}
    assert PingStage().name == "ping" and PingStage().deps == ()
