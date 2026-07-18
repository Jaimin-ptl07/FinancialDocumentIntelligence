"""Retrieval foundations (pure; no I/O, config, clock, RNG).

build_chunks joins a document's `ast` (section body text), `semantic`
(section_type), and `provenance` (page) into one per-document `chunks` record --
one chunk per AST section, under the shared identity `s{i}` that EmbeddingStage/
IndexStage/Milvus already use. Every text-returning retrieval tool resolves a hit
id back to text via this record. build_vector_results (RT-2) is the first such
consumer: pure filter/join/truncate of an already-fetched vector-store hit list.
fuse (RT-4) is the second: pure Reciprocal Rank Fusion over RT-2's vector_search
and RT-3's keyword_search hit lists. Reads inputs as plain Mappings -- no import
of fdi.core.ast/semantic/provenance, no import of fdi.platform.*/fdi.pipeline.*.
Never raises: malformed/partial input degrades per-field (section_type/page ->
None).
"""

from collections.abc import Mapping, Sequence


def _int_or_none(value: object) -> int | None:
    return value if isinstance(value, int) and not isinstance(value, bool) else None


def build_chunks(
    ast: Mapping[str, object],
    semantic: Mapping[str, object],
    provenance: Mapping[str, object],
) -> dict[str, object]:
    page_by_node: dict[str, int] = {}
    node_prov = provenance.get("node_provenance")
    if isinstance(node_prov, list):
        for np in node_prov:
            if not isinstance(np, Mapping):
                continue
            node_id = np.get("node_id")
            page = _int_or_none(np.get("page"))
            if isinstance(node_id, str) and page is not None:
                page_by_node[node_id] = page

    sem_raw = semantic.get("sections")
    sem_sections = sem_raw if isinstance(sem_raw, list) else []

    sections_raw = ast.get("sections")
    sections = sections_raw if isinstance(sections_raw, list) else []

    chunks: list[dict[str, object]] = []
    k = 0  # position among EMITTED chunks (indexes into sem_sections)
    for i, section in enumerate(sections):
        if not isinstance(section, Mapping):
            continue  # consumes index i (keeps s{i} aligned with EmbeddingStage), emits nothing
        heading_raw = section.get("heading")
        heading = heading_raw if isinstance(heading_raw, str) else ""
        paragraphs_raw = section.get("paragraphs")
        paragraphs = (
            [p for p in paragraphs_raw if isinstance(p, str)]
            if isinstance(paragraphs_raw, list)
            else []
        )
        chunk_id = f"s{i}"
        text = heading + "\n" + "\n".join(paragraphs)

        section_type: str | None = None
        if k < len(sem_sections) and isinstance(sem_sections[k], Mapping):
            t = sem_sections[k].get("type")
            section_type = t if isinstance(t, str) else None

        chunks.append({
            "chunk_id": chunk_id,
            "heading": heading_raw if isinstance(heading_raw, str) else None,
            "text": text,
            "section_type": section_type,
            "page": page_by_node.get(chunk_id),
            "para_count": len(paragraphs),
        })
        k += 1

    return {"schema_version": 1, "chunks": chunks}


def build_vector_results(
    hits: list[Mapping[str, object]],
    resolved: Mapping[str, Mapping[str, object]],
    docs: Mapping[str, Mapping[str, object]],
    *,
    section_type: str | None,
    doc_key: str | None,
    version: int | None,
    k: int,
) -> list[dict[str, object]]:
    """Pure post-processing of an ALREADY-FETCHED vector-store hit list (RT-2).
    `hits` is VectorStore.search's shape ({id, score, metadata}, best-match-
    first); `resolved` is a resolve_chunks() result; `docs` is a plain
    {doc_id: {"doc_key": ..., "version": ...}} dict the caller already read
    from Postgres -- no ORM type crosses into core. Filters, joins citations,
    truncates to k WITHOUT re-sorting (output order is a sub-sequence of the
    input order). A hit is dropped, not erred, when: section_type is set and
    doesn't match; doc_key/version is set and the hit's Document row is
    missing or doesn't match; or its chunk id is absent from `resolved`. No I/O.
    """
    results: list[dict[str, object]] = []
    for hit in hits:
        full_id = str(hit.get("id"))

        if section_type is not None:
            meta = hit.get("metadata")
            hit_type = meta.get("section_type") if isinstance(meta, Mapping) else None
            if hit_type != section_type:
                continue

        doc_id = full_id.split(":", 1)[0] if ":" in full_id else full_id
        doc = docs.get(doc_id)
        if doc_key is not None and (doc is None or doc.get("doc_key") != doc_key):
            continue
        if version is not None and (doc is None or doc.get("version") != version):
            continue

        rec = resolved.get(full_id)
        if rec is None:
            continue

        score = hit.get("score")
        results.append({
            "doc_id": rec.get("doc_id"),
            "doc_key": doc.get("doc_key") if doc is not None else None,
            "version": doc.get("version") if doc is not None else None,
            "chunk_id": rec.get("chunk_id"),
            "heading": rec.get("heading"),
            "page": rec.get("page"),
            "section_type": rec.get("section_type"),
            "text": rec.get("text"),
            "score": float(score) if isinstance(score, int | float) else 0.0,
        })
        if len(results) >= k:
            break

    return results


def chunk_rows_for_index(
    *,
    doc_id: str,
    doc_key: str | None,
    version: int | None,
    chunks: Sequence[Mapping[str, object]],
) -> list[dict[str, object]]:
    """From RT-1's `chunks` artifact records, build the flat rows
    `index_chunks_fts` upserts into the `chunk` table. Pure; skips a record
    with a missing/non-str/empty chunk_id, degrades heading/section_type/page
    to None and text to "" on a malformed value. Never raises."""
    rows: list[dict[str, object]] = []
    for c in chunks:
        chunk_id = c.get("chunk_id")
        if not isinstance(chunk_id, str) or not chunk_id:
            continue
        heading = c.get("heading")
        text = c.get("text")
        section_type = c.get("section_type")
        rows.append({
            "id": f"{doc_id}:{chunk_id}",
            "doc_id": doc_id,
            "chunk_id": chunk_id,
            "doc_key": doc_key,
            "version": version,
            "heading": heading if isinstance(heading, str) else None,
            "text": text if isinstance(text, str) else "",
            "section_type": section_type if isinstance(section_type, str) else None,
            "page": _int_or_none(c.get("page")),
        })
    return rows


_RRF_K = 60


def _citation_richness(hit: Mapping[str, object]) -> int:
    return sum(
        1
        for key in ("doc_key", "version", "heading", "page", "section_type")
        if hit.get(key) is not None
    )


def fuse(
    vector_hits: list[Mapping[str, object]],
    keyword_hits: list[Mapping[str, object]],
    *,
    k: int,
    rrf_k: int = _RRF_K,
) -> list[dict[str, object]]:
    """Reciprocal Rank Fusion (RRF) over two best-first, citation-shaped hit
    lists (RT-2's vector_search / RT-3's keyword_search output shape --
    {doc_id, doc_key, version, chunk_id, heading, page, section_type, text,
    score}). Each hit is identified by "{doc_id}:{chunk_id}" (roadmap
    cross-cutting decision 1; computed here, not read off a field). A hit's
    fused score is the sum, over every list it appears in, of
    1/(rrf_k + rank), rank 1-based -- a hit found by both lists therefore
    routinely outranks one found by only one. Output is sorted by fused
    score descending, ties broken by the "{doc_id}:{chunk_id}" string
    ascending (deterministic; never relies on input order or sort stability
    once scores tie), truncated to k. When the same chunk appears in both
    lists with citations of different completeness, the RICHEST citation
    (most non-None among doc_key/version/heading/page/section_type) is kept
    on the fused record; a tie keeps whichever hit was seen first --
    vector_hits before keyword_hits, this function's own argument order. A
    hit missing a str doc_id/chunk_id is skipped, not erred. Pure, total,
    never raises. No I/O."""
    scores: dict[str, float] = {}
    richest: dict[str, Mapping[str, object]] = {}
    for hits in (vector_hits, keyword_hits):
        for rank, hit in enumerate(hits, start=1):
            doc_id = hit.get("doc_id")
            chunk_id = hit.get("chunk_id")
            if not isinstance(doc_id, str) or not isinstance(chunk_id, str):
                continue
            full_id = f"{doc_id}:{chunk_id}"
            scores[full_id] = scores.get(full_id, 0.0) + 1.0 / (rrf_k + rank)
            current = richest.get(full_id)
            if current is None or _citation_richness(hit) > _citation_richness(current):
                richest[full_id] = hit

    ordered_ids = sorted(scores, key=lambda fid: (-scores[fid], fid))
    results: list[dict[str, object]] = []
    for full_id in ordered_ids[:k]:
        rec = dict(richest[full_id])
        rec["score"] = scores[full_id]
        results.append(rec)
    return results
