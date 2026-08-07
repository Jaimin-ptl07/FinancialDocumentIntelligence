"""A stage whose behaviour depends on an injected SERVICE must fold that service
into its cache key, or the framework serves a stale artifact forever."""

import inspect
import re

import pytest

from fdi.pipeline.registry import REGISTRY
from fdi.pipeline.stages.fog_mapping import FogMappingStage
from fdi.pipeline.stages.fog_reason import FogReasonStage

SCHEMA_READERS = (FogMappingStage, FogReasonStage)


@pytest.mark.parametrize("cls", SCHEMA_READERS)
def test_a_schema_change_changes_the_key_material(cls):
    stage = cls()
    a = stage.key_material({"schema": {"modules": [{"module_id": "pl"}]}})
    b = stage.key_material({"schema": {"modules": [{"module_id": "bs"}]}})
    assert a and b and a != b


@pytest.mark.parametrize("cls", SCHEMA_READERS)
def test_the_same_schema_yields_identical_key_material(cls):
    """Otherwise every run misses the cache and re-bills the only paid stage."""
    stage = cls()
    schema = {"modules": [{"module_id": "pl", "retrieval_units": []}]}
    assert stage.key_material({"schema": schema}) == stage.key_material({"schema": schema})


@pytest.mark.parametrize("cls", SCHEMA_READERS)
def test_key_material_does_not_depend_on_dict_ordering(cls):
    stage = cls()
    a = stage.key_material({"schema": {"a": 1, "b": 2}})
    b = stage.key_material({"schema": {"b": 2, "a": 1}})
    assert a == b


@pytest.mark.parametrize("cls", SCHEMA_READERS)
def test_an_absent_schema_yields_stable_empty_material(cls):
    assert cls().key_material({}) == cls().key_material({})


@pytest.mark.parametrize("cls", SCHEMA_READERS)
def test_key_material_never_raises_on_a_junk_service_bag(cls):
    for bag in ({}, {"schema": None}, {"schema": "not a mapping"}, {"schema": [1, 2]}):
        assert isinstance(cls().key_material(bag), str)


def test_both_schema_readers_agree_on_the_same_schema():
    """They read ONE service, so they must invalidate on the SAME edit. If they
    ever disagree, one of them can be re-keyed by a schema change while the
    other keeps serving output computed under the previous taxonomy."""
    schema = {"modules": [{"module_id": "pl"}]}
    assert FogMappingStage().key_material({"schema": schema}) == (
        FogReasonStage().key_material({"schema": schema})
    )


# Services that are CONNECTIONS or SINKS, not data. Their identity is a live
# client object or an accumulator, and hashing it would mean either a constant
# (useless) or an address (a cache miss every run). A stage reading only these
# is correctly exempt from key_material.
#
# Known residual risk, recorded rather than hidden: `embedder` and `ocr` carry
# a MODEL choice that does change their stage's output. That choice comes from
# Settings and changes only on redeploy, so it is not per-run cache poisoning,
# but swapping the embedding model without bumping EmbeddingStage.version WOULD
# serve vectors from the old model. Out of scope here; see the FOG review list.
NON_DATA_SERVICES = frozenset(
    {"embedder", "extractor", "graph_store", "ocr", "semantic", "usage", "vector_store"}
)

_SERVICE_READ = re.compile(r"""services(?:\.get\(|\[)["']([a-z_]+)["']""")


def test_every_stage_reading_a_DATA_service_declares_key_material():
    """The rule, enforced over the WHOLE registry rather than the one stage
    that happened to break it.

    `run.py` derives input_hash from each dependency's OUTPUT hash, so a
    service is invisible to the cache key unless the stage names it. A stage
    reading a data-valued service without a key_material serves output computed
    under the old data forever -- `fog_reason` did exactly this with the
    taxonomy schema, while its own module docstring stated the principle.

    Failing here means one of two things, and BOTH need a human decision: the
    stage reads new data and must declare it, or the service is a client/sink
    and belongs in NON_DATA_SERVICES with a reason."""
    offenders = {}
    for stage in REGISTRY.all().values():
        try:
            source = inspect.getsource(type(stage).run)
        except (OSError, TypeError):  # pragma: no cover - defensive
            continue
        data_services = set(_SERVICE_READ.findall(source)) - NON_DATA_SERVICES
        if data_services and not callable(getattr(stage, "key_material", None)):
            offenders[stage.name] = sorted(data_services)
    assert offenders == {}
