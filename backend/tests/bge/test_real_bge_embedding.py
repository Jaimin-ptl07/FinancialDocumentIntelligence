"""Opt-in smoke test that actually loads the real BAAI/bge-small-en-v1.5
model via fastembed and embeds text.

Skipped unless FDI_RUN_BGE_TEST=1 is set. Unlike the azure/neo4j/milvus
opt-in tests, BGE needs no endpoint/key/URI -- BgeEmbeddingClient.from_settings
never returns None, so there is no "unconfigured" signal to gate on.
FDI_RUN_BGE_TEST is a test-selection-only flag (not a Settings field, never
reaches the app) that exists purely so this test's real cost -- a ~130 MB
one-time model download on first run plus real CPU inference -- stays opt-in
and never runs unattended in CI. Also excluded from the default suite via
`-m "not ... and not bge"`; this skipif is defense-in-depth in case that flag
is ever forgotten (same shape as the azure/neo4j/milvus opt-in tests' skipif).
"""

import os

import pytest

from fdi.platform.config import get_settings
from fdi.platform.embeddings import BgeEmbeddingClient

pytestmark = [
    pytest.mark.bge,
    pytest.mark.skipif(
        os.getenv("FDI_RUN_BGE_TEST") != "1",
        reason="real BGE model load/download not requested (set FDI_RUN_BGE_TEST=1)",
    ),
]


def test_real_bge_embedding_matches_configured_dim() -> None:
    settings = get_settings()
    client = BgeEmbeddingClient.from_settings(settings)
    vectors = client.embed(["Balance Sheet\nTotal Assets 1,00,000\nTotal Liabilities 1,00,000"])
    assert len(vectors) == 1
    assert len(vectors[0]) == settings.embed_dim
