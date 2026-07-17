"""AzureOpenAIEmbeddingClient -- EmbeddingClient backed by Azure OpenAI embeddings.

Lives in `platform` (does I/O), not `pipeline`: the `EmbeddingClient` protocol,
`EmbeddingStage`, and `FakeEmbeddingClient` live in `fdi.pipeline`. This module
structurally satisfies that protocol without importing it -- `platform` may not
import `pipeline` per the layered architecture contract.

`AzureOpenAI` is imported at module level (rather than inside `embed`) so tests
can monkeypatch `fdi.platform.embeddings.AzureOpenAI` with a fake -- no live
Azure OpenAI call in the suite.
"""

from openai import AzureOpenAI

from fdi.platform.config import Settings


class AzureOpenAIEmbeddingClient:
    """EmbeddingClient backed by Azure OpenAI. Lives in platform (does I/O)."""

    def __init__(
        self, *, endpoint: str, key: str, deployment: str, api_version: str, dim: int
    ) -> None:
        self._sdk = AzureOpenAI(azure_endpoint=endpoint, api_key=key, api_version=api_version)
        self.model = deployment
        self._dim = dim

    def embed(self, texts: list[str]) -> list[list[float]]:
        resp = self._sdk.embeddings.create(model=self.model, input=texts, dimensions=self._dim)
        return [d.embedding for d in resp.data]

    @staticmethod
    def from_settings(settings: "Settings") -> "AzureOpenAIEmbeddingClient | None":
        if not settings.embed_endpoint or not settings.embed_key:
            return None
        return AzureOpenAIEmbeddingClient(
            endpoint=settings.embed_endpoint,
            key=settings.embed_key,
            deployment=settings.embed_deployment,
            api_version=settings.embed_api_version,
            dim=settings.embed_dim,
        )
