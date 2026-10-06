from typing import cast

import pytest

from fdi.platform import vectorstore as vs


def test_milvus_store_creates_collection_then_upserts(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: dict[str, object] = {}

    class _FakeSchema:
        def __init__(self) -> None:
            self.fields: list[dict[str, object]] = []
            self.functions: list[object] = []

        def add_field(self, field_name: str, datatype: object, **kw: object) -> None:
            self.fields.append({"name": field_name, "type": datatype, **kw})

        def add_function(self, fn: object) -> None:
            self.functions.append(fn)

    class _FakeIndexParams:
        def __init__(self) -> None:
            self.indexes: list[dict[str, object]] = []

        def add_index(self, field_name: str, **kw: object) -> None:
            self.indexes.append({"field_name": field_name, **kw})

    class _FakeClient:
        def __init__(self, uri: str) -> None:
            calls["uri"] = uri
            self._exists = False

        def has_collection(self, collection_name: str) -> bool:
            calls["has_collection_name"] = collection_name
            return self._exists

        def create_schema(self, **kw: object) -> _FakeSchema:
            return _FakeSchema()

        def prepare_index_params(self) -> _FakeIndexParams:
            return _FakeIndexParams()

        def create_collection(self, **kwargs: object) -> None:
            calls["create_collection"] = kwargs
            self._exists = True

        def upsert(self, **kwargs: object) -> None:
            calls["upsert"] = kwargs

        def flush(self, collection_name: str) -> None:
            calls["flush_collection_name"] = collection_name

        def search(self, **kwargs: object) -> list[list[dict[str, object]]]:
            calls["search"] = kwargs
            return [[{"id": "doc1:s0", "distance": 0.99, "entity": {"heading": "Balance Sheet"}}]]

    monkeypatch.setattr(vs, "MilvusClient", _FakeClient)

    store = vs.MilvusVectorStore(uri="http://h:19530", collection="fdi_sections", dim=4)
    assert calls["uri"] == "http://h:19530"

    receipt = store.upsert(
        [
            {
                "id": "doc1:s0",
                "vector": [0.1, 0.2, 0.3, 0.4],
                "metadata": {"heading": "Balance Sheet"},
            }
        ]
    )
    assert receipt == {"upserted": 1}
    assert calls["create_collection"]["collection_name"] == "fdi_sections"

    # schema-based creation (schema v2), not the old dimension=/metric_type=
    # top-level kwargs -- the BM25 function and analyzer can only be declared
    # at creation time, which forced this off the simple create_collection form.
    schema = cast(_FakeSchema, calls["create_collection"]["schema"])
    names = {f["name"] for f in schema.fields}
    assert {"id", "vector", "text", "sparse", "doc_id", "chunk_id",
            "section_type", "heading", "page"} <= names
    assert len(schema.functions) == 1

    index_params = cast(_FakeIndexParams, calls["create_collection"]["index_params"])
    index_field_names = {i["field_name"] for i in index_params.indexes}
    assert index_field_names == {"vector", "sparse"}

    assert calls["upsert"]["data"] == [
        {"id": "doc1:s0", "vector": [0.1, 0.2, 0.3, 0.4], "heading": "Balance Sheet"}
    ]

    hits = store.search([0.1, 0.2, 0.3, 0.4], 1)
    assert hits == [{"id": "doc1:s0", "score": 0.99, "metadata": {"heading": "Balance Sheet"}}]
    assert calls["search"]["limit"] == 1
    assert calls["search"]["anns_field"] == "vector"
    # NEVER ["*"]: Milvus refuses to output a sparse vector field
    assert "*" not in cast(list[str], calls["search"]["output_fields"])


def test_milvus_store_skips_empty_upsert(monkeypatch: pytest.MonkeyPatch) -> None:
    touched: list[str] = []

    class _FakeClient:
        def __init__(self, uri: str) -> None:
            pass

        def has_collection(self, name: str) -> bool:
            touched.append("has_collection")
            return False

        def create_collection(self, **kwargs: object) -> None:
            touched.append("create_collection")

        def upsert(self, **kwargs: object) -> None:
            touched.append("upsert")

    monkeypatch.setattr(vs, "MilvusClient", _FakeClient)
    store = vs.MilvusVectorStore(uri="http://h:19530", collection="fdi_sections", dim=4)
    assert store.upsert([]) == {"upserted": 0}
    assert touched == []


def test_milvus_store_search_returns_empty_when_collection_missing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class _FakeClient:
        def __init__(self, uri: str) -> None:
            pass

        def has_collection(self, name: str) -> bool:
            return False

    monkeypatch.setattr(vs, "MilvusClient", _FakeClient)
    store = vs.MilvusVectorStore(uri="http://h:19530", collection="fdi_sections", dim=4)
    assert store.search([0.1, 0.2, 0.3, 0.4], k=5) == []


def test_from_settings_none_when_unconfigured() -> None:
    from fdi.platform.config import Settings
    from fdi.platform.vectorstore import MilvusVectorStore

    s = Settings(
        database_url="postgresql+asyncpg://u:p@h:5432/db",
        broker_url="amqp://guest:guest@h:5672/%2F", result_backend="redis://h:6379/0",
        s3_endpoint="h:9000", s3_access_key="ak", s3_secret_key="sk",
        auth_secret="test-signing-secret-0123456789abcdef0123456789abcdef",
    )
    assert MilvusVectorStore.from_settings(s) is None


def test_ensure_collection_declares_bm25_function_and_sparse_index(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    created: dict[str, object] = {}

    class _FakeSchema:
        def __init__(self) -> None:
            self.fields: list[dict[str, object]] = []
            self.functions: list[object] = []

        def add_field(self, field_name: str, datatype: object, **kw: object) -> None:
            self.fields.append({"name": field_name, "type": datatype, **kw})

        def add_function(self, fn: object) -> None:
            self.functions.append(fn)

    class _FakeIndexParams:
        def __init__(self) -> None:
            self.indexes: list[dict[str, object]] = []

        def add_index(self, field_name: str, **kw: object) -> None:
            self.indexes.append({"field_name": field_name, **kw})

    class _FakeClient:
        def __init__(self, uri: str) -> None:
            pass

        def has_collection(self, collection_name: str) -> bool:
            return False

        def create_schema(self, **kw: object) -> _FakeSchema:
            return _FakeSchema()

        def prepare_index_params(self) -> _FakeIndexParams:
            return _FakeIndexParams()

        def create_collection(self, **kw: object) -> None:
            created.update(kw)
            created["functions_at_create"] = len(cast(_FakeSchema, kw["schema"]).functions)

        def upsert(self, **kw: object) -> None:
            pass

        def flush(self, collection_name: str) -> None:
            pass

    monkeypatch.setattr(vs, "MilvusClient", _FakeClient)
    # dim=4, not 384: a hardcoded self._dim would still pass every assertion
    # below unless the schema's `vector` field dim is checked against the
    # exact value the constructor was given.
    store = vs.MilvusVectorStore(uri="http://x", collection="c", dim=4)
    store.upsert([{"id": "d:s1", "vector": [0.0] * 4, "metadata": {"text": "hello"}}])

    schema = created["schema"]
    names = {f["name"] for f in schema.fields}
    assert {"id", "vector", "text", "sparse", "doc_id", "chunk_id",
            "section_type", "heading", "page"} <= names

    vector_field = next(f for f in schema.fields if f["name"] == "vector")
    assert vector_field["dim"] == 4

    text_field = next(f for f in schema.fields if f["name"] == "text")
    assert text_field["enable_analyzer"] is True

    # the BM25 function must be declared BEFORE create_collection -- it cannot
    # be added to a live collection. `_FakeClient.create_collection` snapshots
    # the function count AT THE MOMENT it is called, so this actually enforces
    # ordering (schema.functions alone would still be 1 even if add_function
    # ran after create_collection, since _FakeSchema is a shared mutable
    # object captured by reference).
    assert len(schema.functions) == 1
    assert created["functions_at_create"] == 1, (
        "the BM25 Function must be declared BEFORE create_collection -- "
        "Milvus cannot add one to a live collection"
    )

    index_params = created["index_params"]
    sparse_index = next(i for i in index_params.indexes if i["field_name"] == "sparse")
    assert sparse_index["index_type"] == "SPARSE_INVERTED_INDEX"
    assert sparse_index["metric_type"] == "BM25"
    dense_index = next(i for i in index_params.indexes if i["field_name"] == "vector")
    assert dense_index["metric_type"] == "COSINE"


def test_search_text_passes_raw_text_and_explicit_output_fields(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    seen: dict[str, object] = {}

    class _FakeClient:
        def __init__(self, uri: str) -> None:
            pass

        def has_collection(self, collection_name: str) -> bool:
            return True

        def search(self, **kw: object) -> list[list[dict[str, object]]]:
            seen.update(kw)
            return [[{"id": "d:s1", "distance": 3.5,
                      "entity": {"text": "revenue", "page": 7}}]]

    monkeypatch.setattr(vs, "MilvusClient", _FakeClient)
    store = vs.MilvusVectorStore(uri="http://x", collection="c", dim=384)
    hits = store.search_text("revenue from operations", 5, filter='doc_id == "d"')

    assert seen["data"] == ["revenue from operations"]   # raw text, NOT a vector
    assert seen["anns_field"] == "sparse"
    assert seen["filter"] == 'doc_id == "d"'
    # NEVER ["*"]: Milvus refuses to output a sparse vector field
    assert "*" not in cast(list[str], seen["output_fields"])
    assert "sparse" not in cast(list[str], seen["output_fields"])
    assert "text" in cast(list[str], seen["output_fields"])
    assert hits == [{"id": "d:s1", "score": 3.5,
                     "metadata": {"text": "revenue", "page": 7}}]


def test_search_text_missing_collection_returns_empty(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class _FakeClient:
        def __init__(self, uri: str) -> None:
            pass

        def has_collection(self, collection_name: str) -> bool:
            return False

    monkeypatch.setattr(vs, "MilvusClient", _FakeClient)
    store = vs.MilvusVectorStore(uri="http://x", collection="c", dim=384)
    assert store.search_text("anything", 5) == []
