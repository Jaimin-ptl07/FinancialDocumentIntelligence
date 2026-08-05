"""A stage whose behaviour depends on an injected SERVICE must fold that service
into its cache key, or the framework serves a stale artifact forever."""

from fdi.pipeline.stages.fog_mapping import FogMappingStage


def test_a_schema_change_changes_the_key_material():
    stage = FogMappingStage()
    a = stage.key_material({"schema": {"modules": [{"module_id": "pl"}]}})
    b = stage.key_material({"schema": {"modules": [{"module_id": "bs"}]}})
    assert a and b and a != b


def test_the_same_schema_yields_identical_key_material():
    """Otherwise every run misses the cache and re-bills the only paid stage."""
    stage = FogMappingStage()
    schema = {"modules": [{"module_id": "pl", "retrieval_units": []}]}
    assert stage.key_material({"schema": schema}) == stage.key_material({"schema": schema})


def test_key_material_does_not_depend_on_dict_ordering():
    stage = FogMappingStage()
    a = stage.key_material({"schema": {"a": 1, "b": 2}})
    b = stage.key_material({"schema": {"b": 2, "a": 1}})
    assert a == b


def test_an_absent_schema_yields_stable_empty_material():
    assert FogMappingStage().key_material({}) == FogMappingStage().key_material({})


def test_key_material_never_raises_on_a_junk_service_bag():
    for bag in ({}, {"schema": None}, {"schema": "not a mapping"}, {"schema": [1, 2]}):
        assert isinstance(FogMappingStage().key_material(bag), str)
