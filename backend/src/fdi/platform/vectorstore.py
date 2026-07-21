"""MilvusVectorStore -- VectorStore backed by a real Milvus instance via
pymilvus's MilvusClient.

Lives in `platform` (does I/O), not `pipeline`: the `VectorStore` protocol and
`FakeVectorStore` live in `fdi.pipeline.vectorstore`. This module structurally
satisfies that protocol without importing it -- `platform` may not import
`pipeline` per the layered architecture contract.

`MilvusClient` is imported at module level (rather than inside `__init__`) so
tests can monkeypatch `fdi.platform.vectorstore.MilvusClient` with a fake --
no live Milvus call in the suite.

pymilvus ships NO py.typed marker (design spec sec 2.3) -- every value that
crosses this module's boundary is rebuilt into an explicitly-typed dict by
hand, never returned raw, so mypy --strict's warn_return_any has nothing to
catch here; no blanket ignore is needed."""

from collections.abc import Mapping
from typing import cast

from pymilvus import MilvusClient

from fdi.platform.config import Settings


class MilvusVectorStore:
    """VectorStore backed by Milvus. Lives in platform (does I/O)."""

    def __init__(self, *, uri: str, collection: str, dim: int) -> None:
        self._client = MilvusClient(uri=uri)
        self._collection = collection
        self._dim = dim

    def _ensure_collection(self) -> None:
        if not self._client.has_collection(self._collection):
            self._client.create_collection(
                collection_name=self._collection,
                dimension=self._dim,
                metric_type="COSINE",
                auto_id=False,
                primary_field_name="id",
                id_type="string",
                # Milvus REQUIRES an explicit max_length for a VARCHAR primary
                # key -- without it create_collection fails outright with
                # `type param(max_length) should be specified for the
                # field(id)`, so the first document ever indexed takes the
                # whole index stage down. Ids are f"{doc_id}:{chunk_id}": a
                # 36-char UUID, a colon, and a short chunk id (`s12`, `t3r7`),
                # so ~50 chars in practice. 512 is far above that ceiling and
                # costs nothing -- Milvus stores VARCHAR variably, max_length
                # is a validation bound, not an allocation.
                max_length=512,
                vector_field_name="vector",
            )

    def upsert(self, items: list[Mapping[str, object]]) -> dict[str, object]:
        if not items:
            # No Milvus call at all for an empty batch -- creating a
            # collection for a document with nothing to store is an unforced
            # side effect. Mirrors IndexStage's own zero-chunk skip one layer
            # up (belt-and-suspenders: correct even if ever called directly).
            return {"upserted": 0}
        self._ensure_collection()
        rows = [
            {
                "id": item["id"],
                "vector": item["vector"],
                **dict(cast(Mapping[str, object], item.get("metadata") or {})),
            }
            for item in items
        ]
        self._client.upsert(collection_name=self._collection, data=rows)
        return {"upserted": len(items)}

    def search(self, vector: list[float], k: int) -> list[dict[str, object]]:
        if not self._client.has_collection(self._collection):
            # Nothing has been indexed yet -- the same state FakeVectorStore
            # represents as an empty `_items` dict, not an error. Design
            # spec sec 2.8: fake and real must agree here explicitly.
            return []
        hits = self._client.search(
            collection_name=self._collection,
            data=[vector],
            limit=k,
            output_fields=["*"],
        )
        results: list[dict[str, object]] = []
        for hit in hits[0]:
            entity = dict(cast(Mapping[str, object], hit.get("entity", {})))
            entity.pop("vector", None)
            results.append({"id": hit["id"], "score": hit["distance"], "metadata": entity})
        return results

    @staticmethod
    def from_settings(settings: "Settings") -> "MilvusVectorStore | None":
        if not settings.milvus_uri:
            return None
        return MilvusVectorStore(
            uri=settings.milvus_uri,
            collection=settings.milvus_collection,
            dim=settings.embed_dim,
        )
