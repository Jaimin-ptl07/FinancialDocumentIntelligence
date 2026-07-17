"""Opt-in smoke test against real Azure OpenAI embeddings.

Skipped unless FDI_EMBED_ENDPOINT and FDI_EMBED_KEY are set. Never runs in CI
or in an unconfigured environment. The skipif marker is evaluated at
collection time and does not construct Settings or touch the network, so
collection is always safe.
"""

import os

import pytest

from fdi.platform.config import get_settings
from fdi.platform.embeddings import AzureOpenAIEmbeddingClient

pytestmark = [
    pytest.mark.azure,
    pytest.mark.skipif(
        not (os.getenv("FDI_EMBED_ENDPOINT") and os.getenv("FDI_EMBED_KEY")),
        reason="Azure OpenAI embeddings not configured",
    ),
]


def test_real_embedding_matches_configured_dim() -> None:
    settings = get_settings()
    client = AzureOpenAIEmbeddingClient.from_settings(settings)
    assert client is not None
    vectors = client.embed(["Balance Sheet\nTotal Assets 1,00,000\nTotal Liabilities 1,00,000"])
    assert len(vectors) == 1
    assert len(vectors[0]) == settings.embed_dim
