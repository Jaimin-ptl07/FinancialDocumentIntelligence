import pytest


def test_azure_client_calls_sdk_and_parses_vectors(monkeypatch: pytest.MonkeyPatch) -> None:
    from fdi.platform import embeddings as emb

    calls: dict[str, object] = {}

    class _Datum:
        def __init__(self, vector: list[float]) -> None:
            self.embedding = vector

    class _Resp:
        data = [_Datum([0.1, 0.2, 0.3]), _Datum([0.4, 0.5, 0.6])]

    class _Embeddings:
        def create(self, **kwargs: object) -> _Resp:
            calls["kwargs"] = kwargs
            return _Resp()

    class _Client:
        def __init__(self, **kwargs: object) -> None:
            calls["init"] = kwargs
        embeddings = _Embeddings()

    monkeypatch.setattr(emb, "AzureOpenAI", _Client)
    c = emb.AzureOpenAIEmbeddingClient(
        endpoint="e", key="k", deployment="text-embedding-3-small",
        api_version="2024-10-21", dim=3,
    )
    vectors = c.embed(["Balance Sheet", "Income Statement"])
    assert vectors == [[0.1, 0.2, 0.3], [0.4, 0.5, 0.6]]
    kwargs = calls["kwargs"]
    assert kwargs["model"] == "text-embedding-3-small"
    assert kwargs["input"] == ["Balance Sheet", "Income Statement"]
    assert kwargs["dimensions"] == 3
    assert c.model == "text-embedding-3-small"


def test_from_settings_none_when_unconfigured() -> None:
    from fdi.platform.config import Settings
    from fdi.platform.embeddings import AzureOpenAIEmbeddingClient

    s = Settings(database_url="postgresql+asyncpg://u:p@h:5432/db",
                 broker_url="amqp://guest:guest@h:5672/%2F", result_backend="redis://h:6379/0",
                 s3_endpoint="h:9000", s3_access_key="ak", s3_secret_key="sk")
    assert AzureOpenAIEmbeddingClient.from_settings(s) is None
