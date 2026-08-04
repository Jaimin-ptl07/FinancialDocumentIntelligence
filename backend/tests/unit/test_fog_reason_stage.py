"""Pure stage: no client, no cost, and both artifacts it reads are declared."""

from fdi.core.serialization import canonical_json_bytes

_FOG = {
    "doc_id": "doc-1",
    "bases": {"consolidated": {
        "statements": [], "notes": [],
        "line_items": [{"id": "li:a", "parent": "n", "origin": "note",
                        "label": "Travel", "current": 1000.0}],
        "edges": [],
    }},
}
_MAPPING = {
    "doc_id": "doc-1",
    "bases": {"consolidated": {
        "edges": [{"type": "MAPS_TO", "source": "li:a", "target": "TCEXP"}],
        "proof": [], "unmapped": [],
    }},
}
_SCHEMA = {"modules": []}


def test_the_body_is_keyed_by_basis():
    from fdi.pipeline.stages.fog_reason import build_reasoning

    body = build_reasoning(_FOG, _MAPPING, _SCHEMA)
    assert body["bases"]["consolidated"]["facts"]["TCEXP"] == 1000.0


def test_the_body_carries_doc_id_and_schema_version():
    from fdi.pipeline.stages.fog_reason import build_reasoning

    body = build_reasoning(_FOG, _MAPPING, _SCHEMA)
    assert body["doc_id"] == "doc-1"
    assert body["schema_version"] == 1


def test_every_basis_present_in_the_fog_is_reasoned_over():
    from fdi.pipeline.stages.fog_reason import build_reasoning

    fog = {**_FOG, "bases": {**_FOG["bases"], "standalone": {
        "statements": [], "notes": [], "line_items": [], "edges": []}}}
    body = build_reasoning(fog, _MAPPING, _SCHEMA)
    assert set(body["bases"]) == {"consolidated", "standalone"}


def test_the_body_is_deterministic():
    from fdi.pipeline.stages.fog_reason import build_reasoning

    assert canonical_json_bytes(build_reasoning(_FOG, _MAPPING, _SCHEMA)) == \
        canonical_json_bytes(build_reasoning(_FOG, _MAPPING, _SCHEMA))


def test_malformed_input_never_raises():
    from fdi.pipeline.stages.fog_reason import build_reasoning

    assert build_reasoning({}, {}, {})["bases"] == {}


def test_the_stage_declares_both_artifacts_it_reads():
    import fdi.pipeline.stages  # noqa: F401
    from fdi.pipeline.registry import REGISTRY

    stage = REGISTRY.get("fog_reason")
    assert set(stage.deps) == {"fog_mapping", "fog_objects"}
    assert stage.version == 1


def test_the_dag_orders_fog_reason_last():
    import fdi.pipeline.stages  # noqa: F401
    from fdi.pipeline.registry import REGISTRY
    from fdi.pipeline.resolver import DagResolver

    order = DagResolver(REGISTRY.all()).topological_order()
    assert order.index("fog_mapping") < order.index("fog_reason")
