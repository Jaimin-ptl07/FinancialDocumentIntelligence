"""Opt-in smoke test against a real Milvus instance.

Skipped unless FDI_MILVUS_URI is set. Never runs in CI or in an unconfigured
environment. The skipif marker is evaluated at collection time and does not
construct Settings or touch the network, so collection is always safe."""

import os
import uuid

import pytest
from pymilvus.exceptions import MilvusException

from fdi.platform.config import get_settings
from fdi.platform.vectorstore import MilvusVectorStore

pytestmark = [
    pytest.mark.milvus,
    pytest.mark.skipif(
        not os.getenv("FDI_MILVUS_URI"),
        reason="Milvus not configured",
    ),
]

# Captured at COLLECTION time, which is the only moment this environment still
# exists: tests/conftest.py::_isolate_fdi_env deletes every FDI_ var before each
# test runs. That fixture is right for the rest of the suite and fatal here --
# the skipif above consults the env at collection, so once FDI_MILVUS_URI is set
# the test stops skipping, and then the fixture strips the config it needs and
# `get_settings()` dies with six missing required fields.
#
# The test was therefore UNRUNNABLE by construction, and looked healthy only
# because FDI_MILVUS_URI had never once been set in this project (the same gap
# that left IndexStage silently skipping on every document ever uploaded).
# _isolate_fdi_env's own docstring names the remedy: "Tests that want an FDI_
# var set it themselves afterwards; monkeypatch applies in fixture order, so
# this runs first and they still win."
_COLLECTION_ENV = {k: v for k, v in os.environ.items() if k.upper().startswith("FDI_")}


@pytest.fixture(autouse=True)
def _restore_fdi_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """Put the real FDI_ environment back, after the global fixture cleared it."""
    for key, value in _COLLECTION_ENV.items():
        monkeypatch.setenv(key, value)
    get_settings.cache_clear()


def test_real_upsert_and_search_finds_the_match() -> None:
    # FDI_MILVUS_URI is `milvus:19530` -- the COMPOSE-NETWORK name, which is
    # correct for the api/worker containers and unresolvable from the host. A
    # host-side run of a container-shaped .env is exactly the "unconfigured
    # environment" this module's docstring promises never to fail in, so an
    # unreachable server skips rather than reporting a defect in code it never
    # reached. Point FDI_MILVUS_URI at localhost:19530 to actually run it.
    try:
        store = MilvusVectorStore.from_settings(get_settings())
    except MilvusException as exc:  # pragma: no cover - environment-dependent
        pytest.skip(f"Milvus unreachable at the configured URI: {exc}")
    if store is None:  # pragma: no cover - environment-dependent
        # `from_settings` now ABSORBS an unreachable server and returns None,
        # so the except above no longer fires: a Milvus that is merely down
        # must not fail the API at lifespan. None is therefore the same
        # "not available here" signal the exception used to be.
        pytest.skip("Milvus not reachable at the configured URI")

    doc_id = f"sp14-smoke-{uuid.uuid4()}"
    dim = get_settings().embed_dim
    match_vector = [1.0] + [0.0] * (dim - 1)
    other_vector = [0.0] * (dim - 1) + [1.0]
    items = [
        {"id": f"{doc_id}:s0", "vector": match_vector, "metadata": {"heading": "Balance Sheet"}},
        {"id": f"{doc_id}:s1", "vector": other_vector, "metadata": {"heading": "Notes"}},
    ]
    receipt = store.upsert(items)
    assert receipt == {"upserted": 2}

    hits = store.search(match_vector, k=1)
    assert hits[0]["id"] == f"{doc_id}:s0"
