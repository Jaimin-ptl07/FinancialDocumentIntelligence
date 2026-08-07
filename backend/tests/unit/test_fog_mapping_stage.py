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


# MEASURED ON TCS: the balance sheet prints "Segment revenue" (stand-in for
# the real "Lease liabilities") TWICE under one face unit, with nothing in
# the taxonomy close enough to settle either lexically -- so both rows must
# survive to the LLM tier as TWO rows, not collapse into one.
_SCHEMA_DUP = {
    "modules": [{
        "module_id": "profit_and_loss", "statement": "profit_and_loss",
        "retrieval_units": [{"unit_id": "RU_DUP", "fields": [
            {"tag": "SEGA", "field_name": "Segment A revenue", "origin": "extracted"},
            {"tag": "SEGB", "field_name": "Segment B revenue", "origin": "extracted"},
        ]}],
    }]
}

_FOG_DUP = {
    "doc_id": "doc-dup",
    "bases": {"consolidated": {
        "statements": [{"id": "st:consolidated:profit_and_loss",
                        "statement": "profit_and_loss"}],
        "notes": [],
        "line_items": [
            {"id": "li:s0", "parent": "st:consolidated:profit_and_loss",
             "origin": "face", "label": "Segment revenue", "current": 100.0},
            {"id": "li:s1", "parent": "st:consolidated:profit_and_loss",
             "origin": "face", "label": "Segment revenue", "current": 200.0},
        ],
        "edges": [],
    }},
    "diagnostics": {},
}


async def test_two_rows_sharing_a_label_resolve_to_two_different_concepts():
    """The end-to-end proof of the rekey: "Segment revenue" appears twice in
    ONE unit, neither settles lexically (a tie), both ride the SAME residue
    into ONE LLM call, and the reply -- keyed by ROW NUMBER, not by the
    shared label -- assigns each its own tag. Before the fix, the second row
    silently overwrote the first at every one of these tables."""
    from fdi.pipeline.stages.fog_mapping import build_mapping

    client = FakeExtractorClient([{"1": "SEGA", "2": "SEGB"}])
    body = build_mapping(_FOG_DUP, _SCHEMA_DUP, client=client, store=None)
    proof = {p["line_item"]: p for p in body["bases"]["consolidated"]["proof"]}
    assert proof["li:s0"]["tag"] == "SEGA"
    assert proof["li:s1"]["tag"] == "SEGB"
    assert len(client.calls) == 1


async def test_a_label_the_free_tiers_settle_costs_no_llm_call():
    """"Travel expenses" is an exact alias of TCEXP."""
    client = FakeExtractorClient([{"1": "FACEXP"}])
    body = await _run(client)
    proof = {p["label"]: p for p in body["bases"]["consolidated"]["proof"]}
    # resolve_lexical (fdi.core.fog_mapping) qualifies its tier as
    # "lexical:name" or "lexical:alias" -- the tier, not the exact suffix.
    assert proof["Travel expenses"]["method"].startswith("lexical")


async def test_the_residue_goes_to_the_model_and_becomes_an_edge():
    client = FakeExtractorClient([{"1": "FACEXP"}])
    body = await _run(client)
    proof = {p["label"]: p for p in body["bases"]["consolidated"]["proof"]}
    assert proof["Facility expenses"]["method"] == "llm"
    assert proof["Facility expenses"]["tag"] == "FACEXP"


async def test_a_label_nothing_resolves_stays_visible_as_unmapped():
    """The whole point of the push model: it must not vanish."""
    client = FakeExtractorClient([{"1": "unmapped"}])
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
    client = FakeExtractorClient([{"1": "FACEXP"}])
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


async def test_a_failing_call_is_attempted_but_not_billed():
    """`llm_calls` (attempted) and `llm_calls_billed` (returned usage) used to
    be one conflated count, so this artifact and `GET /documents/{doc_id}/cost`
    disagreed about how many calls were actually billed."""
    class Boom:
        calls: list[tuple[str, str]] = []

        def extract(self, *, system: str, user: str):
            raise RuntimeError("azure is down")

    body = await _run(Boom())
    d = body["diagnostics"]["consolidated"]
    assert d["llm_calls"] == 1
    assert d["llm_calls_billed"] == 0


async def test_a_result_without_usage_degrades_the_unit_not_raises():
    """Decision 10: `result.usage` used to be read OUTSIDE the model call's
    try/except, so a client returning a malformed result -- `.data` present,
    `.usage` missing entirely -- raised past the guard instead of degrading
    just this unit."""
    class NoUsageResult:
        data = {"1": "FACEXP"}
        # deliberately no `.usage` attribute

    class NoUsageClient:
        def extract(self, *, system: str, user: str):
            return NoUsageResult()

    body = await _run(NoUsageClient())
    entry = next(
        u for u in body["bases"]["consolidated"]["unmapped"]
        if u["label"] == "Facility expenses"
    )
    assert entry["reason"] == "model_call_failed"


async def test_prompt_candidates_dropped_is_recorded_when_the_cap_bites():
    """MEASURED: an uncapped note-candidate set ran to ~519 concepts,
    ~12.3k tokens/unit, ~INR 218/document -- `build_map_prompt` was the one
    place that bypassed `MAX_CANDIDATES`. The stage now records what the cap
    cut, so a silent truncation cannot pass as "considered everything"."""
    from fdi.core.concepts import MAX_CANDIDATES
    from fdi.pipeline.stages.fog_mapping import build_mapping

    fields = [
        {"tag": f"T{i}", "field_name": f"Concept {i}", "origin": "extracted"}
        for i in range(MAX_CANDIDATES + 5)
    ]
    schema = {"modules": [{
        "module_id": "profit_and_loss", "statement": "profit_and_loss",
        "retrieval_units": [{"unit_id": "RU", "fields": fields}],
    }]}
    fog = {
        "doc_id": "doc-1",
        "bases": {"consolidated": {
            "statements": [],
            "notes": [{"id": "nt:consolidated:1", "ref": "1", "title": "Many"}],
            "line_items": [{"id": "li:a", "parent": "nt:consolidated:1",
                            "origin": "note", "label": "Unmatched label",
                            "current": 1.0}],
            "edges": [],
        }},
    }
    client = FakeExtractorClient([{"1": "unmapped"}])
    body = build_mapping(fog, schema, client=client, store=None)
    assert body["diagnostics"]["consolidated"]["prompt_candidates_dropped"] == 5


async def test_the_body_is_deterministic():
    from fdi.core.serialization import canonical_json_bytes
    from fdi.pipeline.stages.fog_mapping import build_mapping

    a = build_mapping(_FOG, _SCHEMA,
                      client=FakeExtractorClient([{"1": "FACEXP"}]),
                      store=None)
    b = build_mapping(_FOG, _SCHEMA,
                      client=FakeExtractorClient([{"1": "FACEXP"}]),
                      store=None)
    assert canonical_json_bytes(a) == canonical_json_bytes(b)


def test_the_stage_is_registered_with_the_right_deps():
    import fdi.pipeline.stages  # noqa: F401
    from fdi.pipeline.registry import REGISTRY

    stage = REGISTRY.get("fog_mapping")
    assert stage.deps == ("fog_objects",)
    # v2 added the `schema` usability block.
    # v4 refuses a kind-mismatched or sub-floor lexical match.
    # v5 splits billed calls, caps prompt candidates, scopes memory by basis.
    # v6 units carry each label's `section_path` and the prompt renders it.
    # v7 keys resolution tables by row (item id), not by label.
    # v8 raises the section subset's first-refusal bar to _SECTION_CONFIDENT,
    #    and lets a reply name only a candidate the prompt actually showed.
    assert stage.version == 8


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
                data={"1": "FACEXP"},
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

    client = FakeExtractorClient([{"1": "FACEXP"}])
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
        client=FakeExtractorClient([{"1": "unmapped"}]), store=None,
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
