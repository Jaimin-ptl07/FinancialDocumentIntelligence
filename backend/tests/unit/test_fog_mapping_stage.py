"""The only billed stage. One LLM call per unit, and ONLY for labels the free
tiers could not settle."""

from collections.abc import Mapping

from fdi.pipeline.extractor import FakeExtractorClient

_SCHEMA = {
    "modules": [{
        "module_id": "profit_and_loss", "statement": "profit_and_loss",
        "retrieval_units": [{"unit_id": "RU_PL", "fields": [
            {"tag": "OTEXP", "field_name": "Other expenses", "origin": "extracted",
             "note_aliases": ["other expenses"], "fields": [
                {"tag": "TCEXP", "field_name": "Travel and conveyance",
                 "origin": "extracted", "note_aliases": ["travel expenses"]},
                {"tag": "FACEXP", "field_name": "Facility costs",
                 "origin": "extracted", "note_aliases": []},
             ]},
        ]}],
    }]
}

_FOG = {
    "schema_version": 4, "doc_id": "doc-1",
    "bases": {"consolidated": {
        "statements": [{"id": "st:consolidated:profit_and_loss",
                        "statement": "profit_and_loss"}],
        "notes": [{"id": "nt:consolidated:13", "ref": "13", "title": "Other expenses"}],
        "line_items": [
            {"id": "li:f0", "parent": "st:consolidated:profit_and_loss",
             "origin": "face", "label": "Other expenses", "current": 35230.0},
            {"id": "li:n0", "parent": "nt:consolidated:13", "origin": "note",
             "label": "Travel expenses", "current": 1000.0},
            {"id": "li:n1", "parent": "nt:consolidated:13", "origin": "note",
             "label": "Facility expenses", "current": 2966.0},
        ],
        "edges": [],
    }},
    "diagnostics": {},
}


async def _run(client) -> Mapping[str, object]:
    from fdi.pipeline.stages.fog_mapping import build_mapping

    return build_mapping(_FOG, _SCHEMA, client=client, store=None)


async def test_a_label_the_free_tiers_settle_costs_no_llm_call():
    """"Travel expenses" is an exact alias of TCEXP."""
    client = FakeExtractorClient([{"Facility expenses": "FACEXP"}])
    body = await _run(client)
    proof = {p["label"]: p for p in body["bases"]["consolidated"]["proof"]}
    # resolve_lexical (fdi.core.fog_mapping) qualifies its tier as
    # "lexical:name" or "lexical:alias" -- the tier, not the exact suffix.
    assert proof["Travel expenses"]["method"].startswith("lexical")


async def test_the_residue_goes_to_the_model_and_becomes_an_edge():
    client = FakeExtractorClient([{"Facility expenses": "FACEXP"}])
    body = await _run(client)
    proof = {p["label"]: p for p in body["bases"]["consolidated"]["proof"]}
    assert proof["Facility expenses"]["method"] == "llm"
    assert proof["Facility expenses"]["tag"] == "FACEXP"


async def test_a_label_nothing_resolves_stays_visible_as_unmapped():
    """The whole point of the push model: it must not vanish."""
    client = FakeExtractorClient([{"Facility expenses": "unmapped"}])
    body = await _run(client)
    unmapped = body["bases"]["consolidated"]["unmapped"]
    entry = next(u for u in unmapped if u["label"] == "Facility expenses")
    assert entry["reason"] == "model_said_unmapped"


async def test_no_llm_call_is_made_when_the_free_tiers_settle_everything():
    """A second run of a document with warm memory must be free."""
    fog = {**_FOG, "bases": {"consolidated": {
        **_FOG["bases"]["consolidated"],
        "line_items": [{"id": "li:n0", "parent": "nt:consolidated:13",
                        "origin": "note", "label": "Travel expenses",
                        "current": 1000.0}],
    }}}
    from fdi.pipeline.stages.fog_mapping import build_mapping

    client = FakeExtractorClient([])
    body = build_mapping(fog, _SCHEMA, client=client, store=None)
    assert client.calls == []
    assert body["diagnostics"]["consolidated"]["llm_calls"] == 0


async def test_diagnostics_split_the_tiers():
    client = FakeExtractorClient([{"Facility expenses": "FACEXP"}])
    body = await _run(client)
    d = body["diagnostics"]["consolidated"]
    assert d["by_lexical"] >= 1
    assert d["by_llm"] == 1
    assert d["llm_calls"] == 1


async def test_a_failing_llm_call_degrades_only_that_unit():
    class Boom:
        calls: list[tuple[str, str]] = []

        def extract(self, *, system: str, user: str):
            raise RuntimeError("azure is down")

    body = await _run(Boom())
    # The lexical tier still produced its edge.
    assert body["bases"]["consolidated"]["edges"]
    # A swallowed outage must NOT look like an honest refusal.
    entry = next(
        u for u in body["bases"]["consolidated"]["unmapped"]
        if u["label"] == "Facility expenses"
    )
    assert entry["reason"] == "model_call_failed"


async def test_the_body_is_deterministic():
    from fdi.core.serialization import canonical_json_bytes
    from fdi.pipeline.stages.fog_mapping import build_mapping

    a = build_mapping(_FOG, _SCHEMA,
                      client=FakeExtractorClient([{"Facility expenses": "FACEXP"}]),
                      store=None)
    b = build_mapping(_FOG, _SCHEMA,
                      client=FakeExtractorClient([{"Facility expenses": "FACEXP"}]),
                      store=None)
    assert canonical_json_bytes(a) == canonical_json_bytes(b)


def test_the_stage_is_registered_with_the_right_deps():
    import fdi.pipeline.stages  # noqa: F401
    from fdi.pipeline.registry import REGISTRY

    stage = REGISTRY.get("fog_mapping")
    assert stage.deps == ("fog_objects",)
    # v2 added the `schema` usability block.
    assert stage.version == 3


def test_the_dag_orders_fog_mapping_after_fog_objects():
    import fdi.pipeline.stages  # noqa: F401
    from fdi.pipeline.registry import REGISTRY
    from fdi.pipeline.resolver import DagResolver

    order = DagResolver(REGISTRY.all()).topological_order()
    assert order.index("fog_objects") < order.index("fog_mapping")


async def test_a_recorded_llm_call_appears_on_the_accumulator():
    """The costing addition beyond the brief: a real `.usage` reaches the
    accumulator under source "fog_mapping", tagged for the billed stage."""
    from fdi.core.extractor import ExtractResult
    from fdi.core.pricing import LlmCallUsage
    from fdi.pipeline.stages.fog_mapping import build_mapping
    from fdi.pipeline.usage import UsageAccumulator

    class UsageClient:
        def extract(self, *, system: str, user: str) -> ExtractResult:
            return ExtractResult(
                data={"Facility expenses": "FACEXP"},
                usage=LlmCallUsage(
                    model="gpt-4o", input_tokens=10, output_tokens=5, duration_ms=100
                ),
            )

    usage = UsageAccumulator()
    build_mapping(_FOG, _SCHEMA, client=UsageClient(), store=None, usage=usage)
    assert len(usage.llm_calls) == 1
    assert usage.llm_calls[0].source == "fog_mapping"


async def test_a_none_accumulator_is_harmless():
    """Most callers pass nothing (every test above does) -- costing must
    degrade quietly, never take the mapping result down with it."""
    from fdi.pipeline.stages.fog_mapping import build_mapping

    client = FakeExtractorClient([{"Facility expenses": "FACEXP"}])
    body = build_mapping(_FOG, _SCHEMA, client=client, store=None, usage=None)
    proof = {p["label"]: p for p in body["bases"]["consolidated"]["proof"]}
    assert proof["Facility expenses"]["tag"] == "FACEXP"


def test_a_schema_with_no_concepts_is_recorded_not_silent():
    """MEASURED: the v3.2 seed `load_schema` serves by default carries ZERO
    `tag` keys, so every basis mapped nothing -- 0 edges, 0 facts, 0 cost, no
    failure, and per-basis diagnostics byte-identical to an honest "this
    document discloses nothing". The concept count is what separates "the
    schema is wrong" from "the report is empty"."""
    from fdi.pipeline.stages.fog_mapping import build_mapping

    body = build_mapping(_FOG, {"modules": []}, client=None, store=None)
    assert body["schema"]["usable"] is False
    assert body["schema"]["face_concepts"] == 0


def test_a_usable_schema_reports_its_concept_count():
    from fdi.pipeline.stages.fog_mapping import build_mapping

    body = build_mapping(_FOG, _SCHEMA, client=None, store=None)
    assert body["schema"]["usable"] is True
    assert body["schema"]["face_concepts"] > 0


async def test_a_refusal_and_an_outage_are_distinguishable():
    """They previously produced byte-identical bodies AND diagnostics, so a dead
    Azure endpoint read exactly like a model honestly declining to guess."""
    from fdi.pipeline.stages.fog_mapping import build_mapping

    class Boom:
        calls: list[tuple[str, str]] = []

        def extract(self, *, system: str, user: str):
            raise RuntimeError("azure is down")

    refused = build_mapping(
        _FOG, _SCHEMA,
        client=FakeExtractorClient([{"Facility expenses": "unmapped"}]), store=None,
    )
    outage = build_mapping(_FOG, _SCHEMA, client=Boom(), store=None)

    def reason_for(body, label):
        return next(u["reason"] for u in body["bases"]["consolidated"]["unmapped"]
                    if u["label"] == label)

    assert reason_for(refused, "Facility expenses") == "model_said_unmapped"
    assert reason_for(outage, "Facility expenses") == "model_call_failed"


async def test_no_client_is_its_own_reason():
    from fdi.pipeline.stages.fog_mapping import build_mapping

    body = build_mapping(_FOG, _SCHEMA, client=None, store=None)
    entry = next(u for u in body["bases"]["consolidated"]["unmapped"]
                 if u["label"] == "Facility expenses")
    assert entry["reason"] == "no_client"
