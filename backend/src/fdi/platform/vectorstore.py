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

from pymilvus import DataType, Function, FunctionType, MilvusClient

from fdi.platform.config import Settings

# Explicit, because Milvus REFUSES to output a SPARSE_FLOAT_VECTOR field --
# `output_fields=["*"]` fails the moment `sparse` exists on the schema.
_OUTPUT_FIELDS = ["text", "doc_id", "chunk_id", "section_type", "heading", "page"]

# The largest real chunk measured on the STTL FY2023-24 corpus is 10,253
# chars (section chunks have no size cap; only whole-table chunks are capped,
# at 4000). 65535 is Milvus's VARCHAR ceiling and costs nothing -- max_length
# is a validation bound, not an allocation.
_TEXT_MAX_LENGTH = 65535


class MilvusVectorStore:
    """VectorStore backed by Milvus. Lives in platform (does I/O)."""

    def __init__(self, *, uri: str, collection: str, dim: int) -> None:
        self._client = MilvusClient(uri=uri)
        self._collection = collection
        self._dim = dim

    def _ensure_collection(self) -> None:
        """Schema v2: dense vector AND a BM25 sparse vector derived from `text`.

        The BM25 Function and the analyzer MUST be declared here, at creation
        -- Milvus cannot add either to a live collection. That is why this
        moved off the simple create_collection(dimension=...) form, and why
        the collection name is versioned in config rather than mutated."""
        if self._client.has_collection(self._collection):
            return

        schema = self._client.create_schema(auto_id=False)
        schema.add_field("id", DataType.VARCHAR, is_primary=True, max_length=512)
        schema.add_field("vector", DataType.FLOAT_VECTOR, dim=self._dim)
        schema.add_field(
            "text", DataType.VARCHAR, max_length=_TEXT_MAX_LENGTH,
            enable_analyzer=True, analyzer_params={"type": "standard"},
        )
        schema.add_field("sparse", DataType.SPARSE_FLOAT_VECTOR)
        schema.add_field("doc_id", DataType.VARCHAR, max_length=64)
        schema.add_field("chunk_id", DataType.VARCHAR, max_length=64)
        schema.add_field("section_type", DataType.VARCHAR, max_length=64, nullable=True)
        schema.add_field("heading", DataType.VARCHAR, max_length=1024, nullable=True)
        schema.add_field("page", DataType.INT64, nullable=True)
        schema.add_function(Function(
            name="text_bm25",
            input_field_names=["text"],
            output_field_names=["sparse"],
            function_type=FunctionType.BM25,
        ))

        index_params = self._client.prepare_index_params()
        index_params.add_index(
            field_name="vector", index_type="AUTOINDEX", metric_type="COSINE"
        )
        index_params.add_index(
            field_name="sparse", index_type="SPARSE_INVERTED_INDEX", metric_type="BM25"
        )
        self._client.create_collection(
            collection_name=self._collection, schema=schema, index_params=index_params
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

    def _hits(self, raw: object) -> list[dict[str, object]]:
        rows = cast(list[list[Mapping[str, object]]], raw)
        if not rows:
            return []
        results: list[dict[str, object]] = []
        for hit in rows[0]:
            entity = dict(cast(Mapping[str, object], hit.get("entity", {})))
            results.append(
                {"id": hit["id"], "score": hit["distance"], "metadata": entity}
            )
        return results

    def search(
        self, vector: list[float], k: int, *, filter: str = ""
    ) -> list[dict[str, object]]:
        if not self._client.has_collection(self._collection):
            # Nothing has been indexed yet -- the same state FakeVectorStore
            # represents as an empty `_items` dict, not an error. Design
            # spec sec 2.8: fake and real must agree here explicitly.
            return []
        return self._hits(self._client.search(
            collection_name=self._collection,
            data=[vector],
            anns_field="vector",
            limit=k,
            filter=filter,
            output_fields=_OUTPUT_FIELDS,
        ))

    def search_text(
        self, query: str, k: int, *, filter: str = ""
    ) -> list[dict[str, object]]:
        """BM25 full-text search. `data` is RAW TEXT: Milvus tokenizes it with
        the collection's analyzer and scores against the sparse field
        server-side. No client-side embedding, no tokenization here."""
        if not self._client.has_collection(self._collection):
            return []
        return self._hits(self._client.search(
            collection_name=self._collection,
            data=[query],
            anns_field="sparse",
            limit=k,
            filter=filter,
            output_fields=_OUTPUT_FIELDS,
        ))

    @staticmethod
    def from_settings(settings: "Settings") -> "MilvusVectorStore | None":
        if not settings.milvus_uri:
            return None
        return MilvusVectorStore(
            uri=settings.milvus_uri,
            collection=settings.milvus_collection,
            dim=settings.embed_dim,
        )
