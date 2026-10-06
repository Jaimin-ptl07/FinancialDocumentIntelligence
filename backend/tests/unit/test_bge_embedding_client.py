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
    batch_sizes: list[int] = []

    class _FakeTextEmbedding:
        def __init__(self, model_name: str) -> None:
            construct_calls.append(model_name)

        def embed(self, texts: list[str], batch_size: int) -> list[_FakeVector]:
            # batch_size is REQUIRED, not defaulted: the client must always pass
            # it explicitly. Leaving it to fastembed's default (256) OOMs the
            # ONNX Attention node on a loaded host, which took the whole
            # embedding stage down -- a fake that tolerated its absence would
            # let that regression back in silently.
            embed_calls.append(list(texts))
            batch_sizes.append(batch_size)
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
    # every call carries the bounded batch size, on every invocation
    assert batch_sizes == [emb._EMBED_BATCH_SIZE, emb._EMBED_BATCH_SIZE]
    assert emb._EMBED_BATCH_SIZE <= 64


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
        auth_secret="test-signing-secret-0123456789abcdef0123456789abcdef",
    )
    client = emb.BgeEmbeddingClient.from_settings(s)
    assert client is not None
    assert client.model == "BAAI/bge-small-en-v1.5"
