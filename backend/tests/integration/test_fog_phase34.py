"""fog_objects -> fog_mapping -> fog_reason through the real StageContext."""

from collections.abc import Mapping

import pytest

from fdi.pipeline.extractor import FakeExtractorClient
from fdi.pipeline.stage import StageContext
from fdi.pipeline.stages.fog_mapping import FogMappingStage
from fdi.pipeline.stages.fog_reason import FogReasonStage

pytestmark = pytest.mark.integration

_SCHEMA = {"modules": [{
    "module_id": "profit_and_loss", "statement": "profit_and_loss",
    "retrieval_units": [{"unit_id": "RU", "fields": [
        {"tag": "OTEXP", "field_name": "Other expenses", "origin": "extracted",
         "note_aliases": ["other expenses"], "fields": [
            {"tag": "TCEXP", "field_name": "Travel and conveyance",
             "origin": "extracted", "note_aliases": ["travel expenses"]},
         ]},
    ]}],
}]}
_FOG = {
    "schema_version": 4, "doc_id": "doc-1",
    "bases": {"consolidated": {
        "statements": [], "notes": [
            {"id": "nt:consolidated:13", "ref": "13", "title": "Other expenses"}],
        "line_items": [{"id": "li:a", "parent": "nt:consolidated:13",
                        "origin": "note", "label": "Travel expenses",
                        "current": 1000.0}],
        "edges": [],
    }},
    "diagnostics": {},
}


async def test_the_two_stages_chain_into_facts():
    artifacts: dict[str, object] = {"fog_objects": _FOG}

    async def read(_doc_id: str, stage: str) -> object:
        return artifacts[stage]

    ctx = StageContext(
        doc_id="doc-1", read_artifact=read,
        services={"schema": _SCHEMA, "extractor": FakeExtractorClient([])},
    )
    artifacts["fog_mapping"] = await FogMappingStage().run(ctx)
    reasoning = await FogReasonStage().run(ctx)

    assert isinstance(reasoning, Mapping)
    assert reasoning["bases"]["consolidated"]["facts"]["TCEXP"] == 1000.0


async def test_the_free_tiers_alone_cost_no_llm_call():
    artifacts: dict[str, object] = {"fog_objects": _FOG}

    async def read(_doc_id: str, stage: str) -> object:
        return artifacts[stage]

    client = FakeExtractorClient([])
    ctx = StageContext(
        doc_id="doc-1", read_artifact=read,
        services={"schema": _SCHEMA, "extractor": client},
    )
    body = await FogMappingStage().run(ctx)
    assert client.calls == []
    assert body["diagnostics"]["consolidated"]["llm_calls"] == 0
