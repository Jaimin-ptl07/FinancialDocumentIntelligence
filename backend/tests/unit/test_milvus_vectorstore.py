import pytest


def test_milvus_store_creates_collection_then_upserts(monkeypatch: pytest.MonkeyPatch) -> None:
    from fdi.platform import vectorstore as vs

    calls: dict[str, object] = {}

    class _FakeClient:
        def __init__(self, uri: str) -> None:
            calls["uri"] = uri
            self._exists = False

        def has_collection(self, name: str) -> bool:
            calls["has_collection_name"] = name
            return self._exists

        def create_collection(self, **kwargs: object) -> None:
            calls["create_collection"] = kwargs
            self._exists = True

        def upsert(self, **kwargs: object) -> None:
            calls["upsert"] = kwargs

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
    assert calls["create_collection"]["dimension"] == 4
    assert calls["create_collection"]["metric_type"] == "COSINE"
    assert calls["upsert"]["data"] == [
        {"id": "doc1:s0", "vector": [0.1, 0.2, 0.3, 0.4], "heading": "Balance Sheet"}
    ]

    hits = store.search([0.1, 0.2, 0.3, 0.4], k=1)
    assert hits == [{"id": "doc1:s0", "score": 0.99, "metadata": {"heading": "Balance Sheet"}}]
    assert calls["search"]["limit"] == 1


def test_milvus_store_skips_empty_upsert(monkeypatch: pytest.MonkeyPatch) -> None:
    from fdi.platform import vectorstore as vs

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
    from fdi.platform import vectorstore as vs

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
    )
    assert MilvusVectorStore.from_settings(s) is None
