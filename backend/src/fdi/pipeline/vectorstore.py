"""The VectorStore seam: `upsert(items) -> {"upserted": int}` and
`search(vector, k) -> [{"id", "score", "metadata"}, ...]` (best match first).

`MilvusVectorStore` (platform, does I/O) structurally satisfies this without
importing it -- `platform` may not import `pipeline`. `FakeVectorStore` lives
here for tests: no live Milvus call anywhere in the suite."""

import math
from collections.abc import Mapping
from typing import Protocol, cast, runtime_checkable


@runtime_checkable
class VectorStore(Protocol):
    def upsert(self, items: list[Mapping[str, object]]) -> dict[str, object]: ...

    def search(self, vector: list[float], k: int) -> list[dict[str, object]]: ...


def _cosine(a: list[float], b: list[float]) -> float:
    dot = sum(x * y for x, y in zip(a, b, strict=True))
    norm_a = math.sqrt(sum(x * x for x in a))
    norm_b = math.sqrt(sum(y * y for y in b))
    if norm_a == 0.0 or norm_b == 0.0:
        return 0.0
    return dot / (norm_a * norm_b)


class FakeVectorStore:
    """In-memory VectorStore. `search` does EXACT cosine similarity over every
    stored vector -- correct, not approximate, for the small fixture-scale
    corpora this suite ever indexes; no ANN structure, no network."""

    def __init__(self) -> None:
        self.calls = 0
        self._items: dict[str, Mapping[str, object]] = {}

    def upsert(self, items: list[Mapping[str, object]]) -> dict[str, object]:
        self.calls += 1
        for item in items:
            self._items[str(item["id"])] = item
        return {"upserted": len(items)}

    def search(self, vector: list[float], k: int) -> list[dict[str, object]]:
        scored = [
            {
                "id": item_id,
                "score": _cosine(vector, cast(list[float], item["vector"])),
                "metadata": dict(cast(Mapping[str, object], item.get("metadata") or {})),
            }
            for item_id, item in self._items.items()
        ]
        scored.sort(key=lambda r: cast(float, r["score"]), reverse=True)
        return scored[:k]
