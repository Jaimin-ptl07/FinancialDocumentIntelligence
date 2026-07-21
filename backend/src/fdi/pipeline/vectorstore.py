"""The VectorStore seam: `upsert(items) -> {"upserted": int}` and
`search(vector, k) -> [{"id", "score", "metadata"}, ...]` (best match first).

`MilvusVectorStore` (platform, does I/O) structurally satisfies this without
importing it -- `platform` may not import `pipeline`. `FakeVectorStore` lives
here for tests: no live Milvus call anywhere in the suite."""

import math
import re
from collections.abc import Mapping
from typing import Protocol, cast, runtime_checkable


@runtime_checkable
class VectorStore(Protocol):
    def upsert(self, items: list[Mapping[str, object]]) -> dict[str, object]: ...

    def search(
        self, vector: list[float], k: int, *, filter: str = ""
    ) -> list[dict[str, object]]: ...

    def search_text(
        self, query: str, k: int, *, filter: str = ""
    ) -> list[dict[str, object]]: ...


def _cosine(a: list[float], b: list[float]) -> float:
    dot = sum(x * y for x, y in zip(a, b, strict=True))
    norm_a = math.sqrt(sum(x * x for x in a))
    norm_b = math.sqrt(sum(y * y for y in b))
    if norm_a == 0.0 or norm_b == 0.0:
        return 0.0
    return dot / (norm_a * norm_b)


_FILTER_IN_RE = re.compile(r'\s*(\w+)\s+in\s+\[(.*)\]\s*')
_FILTER_EQ_RE = re.compile(r'\s*(\w+)\s*==\s*"([^"]*)"\s*')


def _matches_filter(metadata: Mapping[str, object], filter: str) -> bool:
    """Understands ONLY the expression shapes this codebase emits:
    `field in ["a", "b"]` (what `_milvus_filter` produces) and
    `field == "value"`, joined by ` and `.

    This is a test double, not a query engine -- an unrecognised shape raises,
    loudly. A fake that silently matched everything on a filter it did not
    understand would make a BROKEN filter look like a passing test, which is
    the failure mode this whole sub-project exists to correct."""
    if not filter.strip():
        return True
    for clause in filter.split(" and "):
        m_in = _FILTER_IN_RE.fullmatch(clause)
        if m_in is not None:
            field = m_in.group(1)
            wanted = {v.strip().strip('"') for v in m_in.group(2).split(",") if v.strip()}
            if str(metadata.get(field)) not in wanted:
                return False
            continue
        m_eq = _FILTER_EQ_RE.fullmatch(clause)
        if m_eq is None:
            raise ValueError(f"FakeVectorStore cannot parse filter clause: {clause!r}")
        if str(metadata.get(m_eq.group(1))) != m_eq.group(2):
            return False
    return True


def _tokenize(text: str) -> set[str]:
    return {t for t in re.split(r"\W+", text.casefold()) if t}


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

    def search(
        self, vector: list[float], k: int, *, filter: str = ""
    ) -> list[dict[str, object]]:
        scored = [
            {
                "id": item_id,
                "score": _cosine(vector, cast(list[float], item["vector"])),
                "metadata": dict(cast(Mapping[str, object], item.get("metadata") or {})),
            }
            for item_id, item in self._items.items()
            if _matches_filter(dict(cast(Mapping[str, object], item.get("metadata") or {})), filter)
        ]
        scored.sort(key=lambda r: cast(float, r["score"]), reverse=True)
        return scored[:k]

    def search_text(
        self, query: str, k: int, *, filter: str = ""
    ) -> list[dict[str, object]]:
        """Deterministic term-overlap stand-in for Milvus BM25. NOT BM25 -- no
        IDF, no length normalisation -- but monotone in the count of query
        terms present, which is the only property the pipeline layer asserts.
        Zero-overlap items are omitted entirely, matching BM25's behaviour of
        never returning a document that shares no term with the query."""
        terms = _tokenize(query)
        if not terms:
            return []
        scored: list[dict[str, object]] = []
        for item_id, item in self._items.items():
            metadata = dict(cast(Mapping[str, object], item.get("metadata") or {}))
            if not _matches_filter(metadata, filter):
                continue
            overlap = len(terms & _tokenize(str(metadata.get("text", ""))))
            if overlap == 0:
                continue
            scored.append({"id": item_id, "score": float(overlap), "metadata": metadata})
        scored.sort(key=lambda r: cast(float, r["score"]), reverse=True)
        return scored[:k]
