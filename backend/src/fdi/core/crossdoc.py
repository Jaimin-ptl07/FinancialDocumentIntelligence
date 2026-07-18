"""Cross-document entity resolution primitives (pure; no I/O, config, clock, RNG).

Documents are grouped into real-world entities (companies) by clustering their
whole-document embedding vectors: single-linkage connected components over the
"cosine >= threshold" graph. A document's vector is the SP-13 per-section chunk
vectors mean-pooled and L2-normalized. Every input is read as a plain
Mapping/Sequence -- no import of fdi.core.knowledge or fdi.core.embeddings, the
same "read defensively, never import the producer's types" posture consolidate/
build_graph take. Never raises: malformed/partial input yields a smaller/emptier
result, never an exception.
"""

import math
from collections.abc import Mapping, Sequence


def _as_float_vector(value: object) -> list[float]:
    """A Sequence of real (non-bool) numbers -> list[float]; anything else -> []."""
    if not isinstance(value, Sequence) or isinstance(value, (str, bytes)):
        return []
    out: list[float] = []
    for x in value:
        if isinstance(x, bool) or not isinstance(x, (int, float)):
            return []
        out.append(float(x))
    return out


def document_vector(embedding: Mapping[str, object]) -> list[float]:
    chunks = embedding.get("chunks")
    if not isinstance(chunks, list):
        return []
    vectors: list[list[float]] = []
    dim: int | None = None
    for chunk in chunks:
        if not isinstance(chunk, Mapping):
            continue
        vec = _as_float_vector(chunk.get("vector"))
        if not vec:
            continue
        if dim is None:
            dim = len(vec)
        if len(vec) != dim:
            continue
        vectors.append(vec)
    if not vectors or dim is None:
        return []
    mean = [sum(v[i] for v in vectors) / len(vectors) for i in range(dim)]
    norm = math.sqrt(sum(c * c for c in mean))
    if norm == 0.0:
        return []
    return [c / norm for c in mean]


def cosine(a: Sequence[float], b: Sequence[float]) -> float:
    if len(a) != len(b):
        return 0.0
    dot = sum(x * y for x, y in zip(a, b, strict=True))
    na = math.sqrt(sum(x * x for x in a))
    nb = math.sqrt(sum(y * y for y in b))
    if na == 0.0 or nb == 0.0:
        return 0.0
    return dot / (na * nb)
