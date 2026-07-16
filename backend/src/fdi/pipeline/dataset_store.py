"""Ground-truth datasets are canonical-JSON artifacts in the object store."""

from fdi.core.eval_types import Dataset, dataset_from_dict, dataset_to_dict
from fdi.platform.storage.artifact_store import ArtifactStore


def dataset_key(name: str, version: int) -> str:
    return f"ground-truth/{name}/v{version}.json"


async def put_dataset(store: ArtifactStore, ds: Dataset) -> str:
    key = dataset_key(ds.name, ds.version)
    await store.put_json(key, dataset_to_dict(ds))
    return key


async def get_dataset(store: ArtifactStore, name: str, version: int) -> Dataset:
    raw = await store.get_json(dataset_key(name, version))
    return dataset_from_dict(raw)
