from fdi.pipeline.embeddings import EmbeddingClient, FakeEmbeddingClient


def test_fake_is_deterministic_per_text() -> None:
    fake = FakeEmbeddingClient(dim=8)
    client: EmbeddingClient = fake
    v1 = client.embed(["Balance Sheet\nAssets 100"])
    v2 = client.embed(["Balance Sheet\nAssets 100"])
    assert v1 == v2
    assert len(v1[0]) == 8
    assert fake.calls == 2
    assert fake.seen == ["Balance Sheet\nAssets 100", "Balance Sheet\nAssets 100"]


def test_fake_differs_across_texts() -> None:
    fake = FakeEmbeddingClient(dim=8)
    v = fake.embed(["Balance Sheet", "Income Statement"])
    assert v[0] != v[1]


def test_fake_embeds_multiple_texts_in_one_call() -> None:
    fake = FakeEmbeddingClient(dim=4)
    vectors = fake.embed(["a", "b", "c"])
    assert len(vectors) == 3
    assert fake.calls == 1


def test_fake_exposes_model_name() -> None:
    fake = FakeEmbeddingClient(model="fake-embedding")
    assert fake.model == "fake-embedding"
