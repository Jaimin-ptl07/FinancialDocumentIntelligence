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


def cluster(vectors: Mapping[str, Sequence[float]], threshold: float) -> list[list[str]]:
    """Single-linkage connected components over the cosine>=threshold graph.

    Deterministic: output is sorted, not incidentally ordered. Union-find attaches
    the larger root under the smaller so each component's root is its min doc_id.
    """
    ids = sorted(vectors)
    parent: dict[str, str] = {i: i for i in ids}

    def find(x: str) -> str:
        root = x
        while parent[root] != root:
            root = parent[root]
        while parent[x] != root:  # path-compress
            parent[x], x = root, parent[x]
        return root

    def union(x: str, y: str) -> None:
        rx, ry = find(x), find(y)
        if rx != ry:
            parent[max(rx, ry)] = min(rx, ry)

    for i in range(len(ids)):
        for j in range(i + 1, len(ids)):
            if cosine(vectors[ids[i]], vectors[ids[j]]) >= threshold:
                union(ids[i], ids[j])

    groups: dict[str, list[str]] = {}
    for doc_id in ids:
        groups.setdefault(find(doc_id), []).append(doc_id)
    return sorted((sorted(g) for g in groups.values()), key=lambda g: g[0])


def _int_or_none(value: object) -> int | None:
    return value if isinstance(value, int) and not isinstance(value, bool) else None


def consolidate_entity(members: list[Mapping[str, object]]) -> dict[str, object]:
    """Fold clustered members ({doc_id, doc_key, version, knowledge}) into the
    entity record. Version-ordered (None last, doc_id tie-break); entity_id = min
    doc_id; degrades on missing/malformed knowledge; never raises."""

    def sort_key(m: Mapping[str, object]) -> tuple[int, int, str]:
        doc_id = m.get("doc_id")
        doc_id_s = doc_id if isinstance(doc_id, str) else ""
        v = _int_or_none(m.get("version"))
        return (0, v, doc_id_s) if v is not None else (1, 0, doc_id_s)

    ordered = sorted(members, key=sort_key)
    doc_ids: list[str] = []
    for m in ordered:
        candidate = m.get("doc_id")
        if isinstance(candidate, str):
            doc_ids.append(candidate)
    entity_id = min(doc_ids) if doc_ids else ""

    member_views: list[dict[str, object]] = []
    concepts_set: set[str] = set()
    concept_presence: dict[str, list[object]] = {}
    quality_trend: list[dict[str, object]] = []

    for m in ordered:
        doc_id = m.get("doc_id")
        doc_id_s = doc_id if isinstance(doc_id, str) else ""
        doc_key = m.get("doc_key")
        version = _int_or_none(m.get("version"))
        knowledge = m.get("knowledge")
        k: Mapping[str, object] = knowledge if isinstance(knowledge, Mapping) else {}
        quality = k.get("quality")
        q: Mapping[str, object] = quality if isinstance(quality, Mapping) else {}
        ok = q.get("ok")
        ok_b = ok if isinstance(ok, bool) else False
        presence_key: object = version if version is not None else doc_id_s

        member_views.append({
            "doc_id": doc_id_s,
            "doc_key": doc_key if isinstance(doc_key, str) else None,
            "version": version,
            "section_count": _int_or_none(k.get("section_count")) or 0,
            "concept_count": _int_or_none(k.get("concept_count")) or 0,
            "quality_ok": ok_b,
        })

        concepts_raw = k.get("concepts")
        if isinstance(concepts_raw, list):
            for c in concepts_raw:
                if isinstance(c, str):
                    concepts_set.add(c)
                    concept_presence.setdefault(c, []).append(presence_key)

        quality_trend.append({
            "version": version,
            "ok": ok_b,
            "error_count": _int_or_none(q.get("error_count")) or 0,
            "warning_count": _int_or_none(q.get("warning_count")) or 0,
        })

    return {
        "schema_version": 1,
        "entity_id": entity_id,
        "member_count": len(ordered),
        "members": member_views,
        "concepts": sorted(concepts_set),
        "concept_presence": {c: concept_presence[c] for c in sorted(concept_presence)},
        "quality_trend": quality_trend,
    }
