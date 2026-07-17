"""The EmbeddingClient seam: an `embed(texts) -> list[list[float]]` protocol.

`AzureOpenAIEmbeddingClient` (platform, does I/O) structurally satisfies this
without importing it -- `platform` may not import `pipeline`. `FakeEmbeddingClient`
lives here for tests: no live Azure OpenAI call anywhere in the suite.
"""

import hashlib
from typing import Protocol, runtime_checkable


@runtime_checkable
class EmbeddingClient(Protocol):
    model: str

    def embed(self, texts: list[str]) -> list[list[float]]: ...


class FakeEmbeddingClient:
    """Deterministic, network-free stand-in. Derives each vector from a sha256
    digest of the text so the SAME text always produces the SAME vector within
    a test run (and across runs), without ever calling out to Azure."""

    def __init__(self, dim: int = 8, model: str = "fake-embedding") -> None:
        self.model = model
        self.dim = dim
        self.calls = 0
        self.seen: list[str] = []

    def embed(self, texts: list[str]) -> list[list[float]]:
        self.calls += 1
        self.seen.extend(texts)
        return [self._vector(t) for t in texts]

    def _vector(self, text: str) -> list[float]:
        digest = hashlib.sha256(text.encode("utf-8")).digest()
        return [b / 255.0 for b in digest[: self.dim]]
