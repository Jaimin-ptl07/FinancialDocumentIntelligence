"""Retrieval foundations (pure; no I/O, config, clock, RNG).

build_chunks joins a document's `ast` (section body text), `semantic`
(section_type), and `provenance` (page) into one per-document `chunks` record --
one chunk per AST section, under the shared identity `s{i}` that EmbeddingStage/
IndexStage/Milvus already use. Every text-returning retrieval tool resolves a hit
id back to text via this record. Reads inputs as plain Mappings -- no import of
fdi.core.ast/semantic/provenance. Never raises: malformed/partial input degrades
per-field (section_type/page -> None).
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
