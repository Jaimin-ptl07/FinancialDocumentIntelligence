"""BgeEmbeddingClient -- EmbeddingClient backed by a local BGE-small model via
fastembed (BAAI/bge-small-en-v1.5, 384-dim, ONNX runtime, CPU).

Lives in `platform` (does I/O -- loads/downloads model weights), not
`pipeline`: the `EmbeddingClient` protocol, `EmbeddingStage`, and
`FakeEmbeddingClient` live in `fdi.pipeline`. This module structurally
satisfies that protocol without importing it -- `platform` may not import
`pipeline` per the layered architecture contract.

`fastembed.TextEmbedding` is imported at module level (rather than inside
`embed`) so tests can monkeypatch `fdi.platform.embeddings.TextEmbedding` with
a fake -- no real model load/download anywhere in the default suite.

The MODEL ITSELF is constructed LAZILY: `TextEmbedding(model_name=...)` is
NOT called in `__init__` or `from_settings`, only on the first `embed()` call,
then cached on the instance. Constructing it eagerly would make wiring this
module -- which now happens UNCONDITIONALLY at every process start, since
`from_settings` never returns None -- download/load ~130 MB of ONNX weights at
API lifespan / every worker task, even for a process that never actually
embeds anything. A document with zero AST sections costs zero model loads:
EmbeddingStage already skips calling `embed()` entirely when there are no
chunks (unchanged from SP-13), and laziness means that skip also skips the
model load.

fastembed DOES ship a py.typed marker (verified directly against the
installed 0.8.0 package's own metadata: `fastembed/py.typed` exists, unlike
pymilvus which SP-14 confirmed ships none) -- but its content is the PEP
561 "partial" marker (`"partial"`), so mypy type-checks against fastembed's
own inline annotations rather than the blanket `Any` an absent/ignored stub
would produce. `TextEmbedding.embed(...)` returns
`Iterable[NDArray[np.float64 | np.float32 | ...]]`; each array's `.tolist()`
is numpy's own (also py.typed, but declared `Any`-returning) conversion to a
plain Python list. Discipline: every value crossing this module's boundary is
rebuilt by hand into a plain `list[float]`, never returned raw, so
warn_return_any has nothing to catch beyond that single, expected `Any` hop
through numpy's `tolist()` -- same discipline as MilvusVectorStore
(fdi/platform/vectorstore.py), no blanket ignore needed.
"""

from fastembed import TextEmbedding

from fdi.platform.config import Settings


class BgeEmbeddingClient:
    """EmbeddingClient backed by a local BGE-small model (fastembed, CPU).
    Lives in platform (does I/O: loads/downloads model weights on first use)."""

    def __init__(self, model: str) -> None:
        self.model = model
        self._impl: TextEmbedding | None = None

    def _get_impl(self) -> TextEmbedding:
        if self._impl is None:
            self._impl = TextEmbedding(model_name=self.model)
        return self._impl

    def embed(self, texts: list[str]) -> list[list[float]]:
        impl = self._get_impl()
        return [vector.tolist() for vector in impl.embed(texts)]

    @staticmethod
    def from_settings(settings: "Settings") -> "BgeEmbeddingClient":
        return BgeEmbeddingClient(settings.embed_model)
