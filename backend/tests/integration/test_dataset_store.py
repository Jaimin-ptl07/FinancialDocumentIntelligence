"""Ground-truth dataset store against a real MinIO.

Datasets are canonical-JSON artifacts under `ground-truth/{name}/v{version}.json`.
The round-trip through `put_dataset`/`get_dataset` is the only thing this suite
checks: that `Dataset`/`EvalCase` values survive a real object-store write and
read byte-for-byte, via `ArtifactStore` + `eval_types` (de)serialization.
"""

from __future__ import annotations

import pytest
from minio import Minio

from fdi.core.eval_types import Dataset, EvalCase
from fdi.pipeline.dataset_store import dataset_key, get_dataset, put_dataset
from fdi.platform.storage.artifact_store import ArtifactStore

pytestmark = pytest.mark.integration


@pytest.fixture
async def store(minio_client: Minio) -> ArtifactStore:
    s = ArtifactStore(minio_client, bucket="test-artifacts")
    await s.ensure_bucket()
    return s


async def test_dataset_roundtrips_through_store(store: ArtifactStore) -> None:
    ds = Dataset("toy", 1, (EvalCase("c1", "a", "A"),))
    key = await put_dataset(store, ds)
    assert key == dataset_key("toy", 1) == "ground-truth/toy/v1.json"
    assert await get_dataset(store, "toy", 1) == ds
