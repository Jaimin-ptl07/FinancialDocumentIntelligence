import pytest


class _FakeVector:
    def __init__(self, values: list[float]) -> None:
        self._values = values

    def tolist(self) -> list[float]:
        return self._values


def test_bge_client_is_lazy_and_caches_the_model(monkeypatch: pytest.MonkeyPatch) -> None:
    from fdi.platform import embeddings as emb

    construct_calls: list[str] = []
    embed_calls: list[list[str]] = []

    class _FakeTextEmbedding:
        def __init__(self, model_name: str) -> None:
            construct_calls.append(model_name)

        def embed(self, texts: list[str]) -> list[_FakeVector]:
            embed_calls.append(list(texts))
            return [_FakeVector([0.1, 0.2, 0.3]) for _ in texts]

    monkeypatch.setattr(emb, "TextEmbedding", _FakeTextEmbedding)

    client = emb.BgeEmbeddingClient("BAAI/bge-small-en-v1.5")
    assert construct_calls == []  # no model construction at __init__ -- lazy

    vectors = client.embed(["Balance Sheet", "Income Statement"])
    assert vectors == [[0.1, 0.2, 0.3], [0.1, 0.2, 0.3]]
    assert construct_calls == ["BAAI/bge-small-en-v1.5"]  # constructed once

    client.embed(["Notes"])
    assert construct_calls == ["BAAI/bge-small-en-v1.5"]  # cached, NOT rebuilt
    assert embed_calls == [["Balance Sheet", "Income Statement"], ["Notes"]]


def test_bge_client_exposes_model_name() -> None:
    from fdi.platform.embeddings import BgeEmbeddingClient

    client = BgeEmbeddingClient("BAAI/bge-small-en-v1.5")
    assert client.model == "BAAI/bge-small-en-v1.5"


def test_from_settings_always_returns_a_client_with_no_model_load(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from fdi.platform import embeddings as emb
    from fdi.platform.config import Settings

    def _boom(*args: object, **kwargs: object) -> None:
        raise AssertionError("TextEmbedding must not be constructed by from_settings")

    monkeypatch.setattr(emb, "TextEmbedding", _boom)

    s = Settings(
        database_url="postgresql+asyncpg://u:p@h:5432/db",
        broker_url="amqp://guest:guest@h:5672/%2F", result_backend="redis://h:6379/0",
        s3_endpoint="h:9000", s3_access_key="ak", s3_secret_key="sk",
    )
    client = emb.BgeEmbeddingClient.from_settings(s)
    assert client is not None
    assert client.model == "BAAI/bge-small-en-v1.5"
