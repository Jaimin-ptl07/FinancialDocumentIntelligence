"""Retrieval foundations (pure; no I/O, config, clock, RNG).

build_chunks joins a document's `ast` (section body text), `semantic`
(section_type), and `provenance` (page) into one per-document `chunks` record --
one chunk per AST section, under the shared identity `s{i}` that EmbeddingStage/
IndexStage/Milvus already use. TR-1 P2 replaced the Phase 3a per-row `t{i}r{r}`
table chunks with ONE table CARD per table (`t{i}`, unchanged id) derived from
`structure` + the `basis` artifact (below) -- a LOCATION summary (identity +
resolved column headers + row labels, no values) rather than a flattened
row-by-row blob, so the reranker sees one candidate per table instead of one
per row. build_vector_results (RT-2) is a pure filter/join/truncate of an
already-fetched vector-store hit list; as of RT-9 the hit itself carries its
text and citation metadata (Milvus stores them), so build_vector_results no
longer consumes a resolve_chunks() result -- there is no per-search artifact
read behind it any more. fuse (RT-4) is the second consumer of this hit shape:
pure Reciprocal Rank Fusion over RT-2's vector_search and RT-3's keyword_search
hit lists. Reads inputs as plain Mappings -- no import of
fdi.core.ast/semantic/provenance, no import of fdi.platform.*/fdi.pipeline.*
(fdi.core.basis's pure, total `page_basis` lookup is the one exception, per
the TR-1 design: basis is a first-class dimension tagged onto every card).
Never raises: malformed/partial input degrades per-field (section_type/page ->
None, basis -> "unknown").

RT-9 retired chunk_rows_for_index along with the Postgres `chunk` table and
index_chunks_fts/keyword_search: keyword search is now Milvus-native BM25
over the same collection vector_search already reads, so there is no
Postgres row-building step left in this module.
"""

import re
from collections.abc import Mapping, Sequence

from fdi.core.basis import page_basis

_STATEMENT_TYPE_ANCHORS: tuple[tuple[str, str], ...] = (
    ("balance sheet", "BS"),
    ("statement of profit and loss", "PL"),
    ("profit and loss", "PL"),
    ("cash flow", "CF"),
)

_UNIT_ANCHORS: tuple[tuple[str, str], ...] = (
    ("lakh", "Rs Lakhs"),
    ("crore", "Rs Crore"),
    ("million", "Rs Million"),
)

_NOTE_LEADING_RE = re.compile(r"^\s*note\s+(\d+)", re.IGNORECASE)
_NUMBER_LEADING_RE = re.compile(r"^\s*(\d+)\.")


def _int_or_none(value: object) -> int | None:
    return value if isinstance(value, int) and not isinstance(value, bool) else None


def _table_context(table: Mapping[str, object], table_index: int) -> str:
    """Priority: table's own `caption.content` (Azure-supplied), else
    `f"Table {table_index}"` -- never blank. (Phase 3a does not resolve the
    owning AST section's heading -- that requires `ast.table_refs`, deferred
    to Phase 3b -- so caption is the first, not second, fallback here.)"""
    caption = table.get("caption")
    if isinstance(caption, Mapping):
        content = caption.get("content")
        if isinstance(content, str) and content.strip():
            return content
    return f"Table {table_index}"


def _table_page(table: Mapping[str, object]) -> int | None:
    """First bounding region's page, mirroring `core.provenance._page_of`'s
    "first region" convention. None if absent/malformed."""
    regions_raw = table.get("bounding_regions")
    if isinstance(regions_raw, list) and regions_raw:
        first = regions_raw[0]
        if isinstance(first, Mapping):
            return _int_or_none(first.get("page_number"))
    return None


def _table_caption_text(table: Mapping[str, object]) -> str:
    """The table's raw `caption.content`, or `""` if absent/malformed -- the
    text `statement_type`/`note_no`/`units` detection runs against (distinct
    from `_table_context`'s `f"Table {i}"` DISPLAY fallback, which must never
    leak into these best-effort detectors)."""
    caption = table.get("caption")
    if isinstance(caption, Mapping):
        content = caption.get("content")
        if isinstance(content, str):
            return content
    return ""


def _statement_type(caption_text: str) -> str:
    """Best-effort primary-statement classification from the table's caption
    text: `"BS"`/`"PL"`/`"CF"` on a matching anchor phrase, else `"unknown"`
    -- never blocks, `"unknown"` is a valid, expected outcome."""
    casefolded = caption_text.casefold()
    for anchor, statement_type in _STATEMENT_TYPE_ANCHORS:
        if anchor in casefolded:
            return statement_type
    return "unknown"


def _note_no(caption_text: str) -> str:
    """A leading `"Note <n>"` or `"<n>."` in the caption -> `str(n)`, else
    `""` (never blocks note-less tables, e.g. primary statements)."""
    match = _NOTE_LEADING_RE.match(caption_text)
    if match:
        return match.group(1)
    match = _NUMBER_LEADING_RE.match(caption_text)
    if match:
        return match.group(1)
    return ""


def _units(caption_text: str) -> str:
    """Best-effort units label from the caption text (`"lakh"`/`"crore"`/
    `"million"` -> `"Rs Lakhs"`/`"Rs Crore"`/`"Rs Million"`), else `""`."""
    casefolded = caption_text.casefold()
    for anchor, units in _UNIT_ANCHORS:
        if anchor in casefolded:
            return units
    return ""


def _table_grid_rows(
    structure_table: Mapping[str, object],
) -> tuple[int, int, list[object], list[object]]:
    """`(n_rows, header_row_count, rows, column_headers)` from a table's
    `grid`, each degrading to `0`/`0`/`[]`/`[]` on malformed input."""
    grid_obj = structure_table.get("grid")
    grid_map = grid_obj if isinstance(grid_obj, Mapping) else {}
    n_rows_raw = grid_map.get("n_rows")
    n_rows = n_rows_raw if isinstance(n_rows_raw, int) else 0
    header_row_count_raw = grid_map.get("header_row_count")
    header_row_count = (
        header_row_count_raw if isinstance(header_row_count_raw, int) else 0
    )
    rows_raw = grid_map.get("grid")
    rows = rows_raw if isinstance(rows_raw, list) else []
    headers_raw = grid_map.get("column_headers")
    column_headers = headers_raw if isinstance(headers_raw, list) else []
    return n_rows, header_row_count, rows, column_headers


def _is_descriptive(cell: object) -> bool:
    """True for a DESCRIPTIVE label cell: a string >3 non-space chars long that
    contains a space OR a lowercase ASCII letter. This admits real labels --
    multi-word (`"Total assets"`, `"TOTAL ASSETS"`) or mixed-case
    (`"Revenue from operations"`) -- while excluding the cells that pollute a
    leading index/number column: roman-numeral tokens of ANY length
    (`I.`, `VIII`, `XIII.` -- no space, no lowercase), note numbers (`22`),
    blanks, and numeric values (`18795.89`). The space-or-lowercase test is
    what makes multi-char roman numerals fail (a plain `any(isalpha)` test
    passes `VIII` because `V/I/X` are letters)."""
    if not isinstance(cell, str):
        return False
    stripped = cell.strip()
    return len(stripped) > 3 and (
        " " in stripped or any("a" <= c <= "z" for c in stripped)
    )


def _label_column_index(
    rows: Sequence[object], header_row_count: int, n_rows: int, n_cols: int
) -> int:
    """Index of the column to read row labels from: the column with the most
    DESCRIPTIVE data-row cells (see `_is_descriptive`). For Indian statement
    tables this is the "Particulars" column, NOT the leading roman-numeral
    index column. Ties resolve to the leftmost; a grid with no descriptive
    column falls back to 0 (the historical behaviour -- never worse). Pure."""
    best, best_score = 0, -1
    for j in range(n_cols):
        score = 0
        for r in range(header_row_count, n_rows):
            row_obj = rows[r] if r < len(rows) else []
            row = row_obj if isinstance(row_obj, list) else []
            if j < len(row) and _is_descriptive(row[j]):
                score += 1
        if score > best_score:
            best_score, best = score, j
    return best


def _cell_text(row: Sequence[object], col: int) -> str:
    cell = row[col] if 0 <= col < len(row) else ""
    return cell if isinstance(cell, str) else ""


def _table_chunks(
    structure: Mapping[str, object], basis: Mapping[str, object]
) -> list[dict[str, object]]:
    """ONE card per table (`chunk_id=f"t{i}"`, `i` the table's position in
    `structure["tables"]` -- TR-1 P2 replaces the old per-row `t{i}r{r}`
    chunks + capped whole-table blob with this single LOCATION-summary card:
    identity line (statement type, basis, note, units, page) + resolved
    column headers + data-row labels -- no cell VALUES, so the card is
    findable/basis-searchable without flooding the reranker the way the old
    per-row fragments did. A `structure` with no/malformed `tables` list
    emits nothing (degrades, never raises); a malformed/missing `basis`
    artifact degrades every card's basis to `"unknown"` via `page_basis`."""
    tables_raw = structure.get("tables")
    tables = tables_raw if isinstance(tables_raw, list) else []

    out: list[dict[str, object]] = []
    for i, table_raw in enumerate(tables):
        if not isinstance(table_raw, Mapping):
            continue
        context = _table_context(table_raw, i)
        page = _table_page(table_raw)
        basis_str = page_basis(basis, page)

        caption_text = _table_caption_text(table_raw)
        statement_type = _statement_type(caption_text)
        note_no = _note_no(caption_text)
        units = _units(caption_text)

        n_rows, header_row_count, rows, column_headers = _table_grid_rows(table_raw)

        n_cols = len(column_headers)
        for row_obj in rows:
            if isinstance(row_obj, list):
                n_cols = max(n_cols, len(row_obj))
        label_col = _label_column_index(rows, header_row_count, n_rows, n_cols)

        # Every header EXCEPT the chosen label column's. When the label column
        # is not column 0, column 0's header (often a duplicated "Particulars"
        # over the index column) stays here -- a harmless extra keyword, never
        # a dropped or duplicated VALUE-column header.
        col_headers_joined = " | ".join(
            h
            for j, h in enumerate(column_headers)
            if j != label_col and isinstance(h, str) and h
        )

        row_labels: list[str] = []
        for r in range(header_row_count, n_rows):
            row_obj = rows[r] if r < len(rows) else []
            row = row_obj if isinstance(row_obj, list) else []
            row_labels.append(_cell_text(row, label_col))
        row_labels_joined = "; ".join(row_labels)

        note_part = f" | note {note_no}" if note_no else ""
        units_part = f" | {units}" if units else ""

        text = (
            f"[{context} | {statement_type} | basis={basis_str}"
            f"{note_part}{units_part} | page {page}]\n"
            f"Columns: {col_headers_joined}\n"
            f"Rows: {row_labels_joined}"
        )

        out.append({
            "chunk_id": f"t{i}",
            "heading": context,
            "text": text,
            "section_type": None,
            "page": page,
            "para_count": None,
        })
    return out


def _dedup_by_text(chunks: list[dict[str, object]]) -> list[dict[str, object]]:
    """One global exact-text pass: keep first occurrence in emission order,
    drop later chunks whose `text` is byte-identical to an earlier one.
    Survivors' ids are NEVER renumbered -- gaps are fine (the already-tested
    `s{i}` id-gap contract; this pass is consistent with it, not a new one)."""
    seen: set[str] = set()
    out: list[dict[str, object]] = []
    for c in chunks:
        text = c.get("text")
        key = text if isinstance(text, str) else ""
        if key in seen:
            continue
        seen.add(key)
        out.append(c)
    return out


def build_chunks(
    ast: Mapping[str, object],
    semantic: Mapping[str, object],
    provenance: Mapping[str, object],
    structure: Mapping[str, object],
    basis: Mapping[str, object],
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

    table_chunks = _table_chunks(structure, basis)
    return {"schema_version": 1, "chunks": _dedup_by_text(chunks + table_chunks)}


def build_vector_results(
    hits: list[Mapping[str, object]],
    docs: Mapping[str, Mapping[str, object]],
    *,
    section_type: str | None,
    doc_key: str | None,
    version: int | None,
    k: int,
) -> list[dict[str, object]]:
    """Pure post-processing of an already-fetched vector-store hit list.

    RT-9: the hit now CARRIES its own text and citation metadata (the Milvus
    collection stores them), so there is no `resolved` chunk-record map any
    more and no artifact read behind it. `docs` remains: the collection is
    filtered on `doc_id` because a Stage has no `doc_key`/`version` to write,
    so the doc_key/version citation is still joined here from a map the
    caller read from Postgres. Filters, joins, truncates to k WITHOUT
    re-sorting. Never raises."""
    results: list[dict[str, object]] = []
    for hit in hits:
        full_id = str(hit.get("id"))
        meta_raw = hit.get("metadata")
        meta = meta_raw if isinstance(meta_raw, Mapping) else {}

        if section_type is not None and meta.get("section_type") != section_type:
            continue

        doc_id = full_id.split(":", 1)[0] if ":" in full_id else full_id
        doc = docs.get(doc_id)
        if doc_key is not None and (doc is None or doc.get("doc_key") != doc_key):
            continue
        if version is not None and (doc is None or doc.get("version") != version):
            continue

        chunk_id = meta.get("chunk_id")
        score = hit.get("score")
        results.append({
            "doc_id": doc_id,
            "doc_key": doc.get("doc_key") if doc is not None else None,
            "version": doc.get("version") if doc is not None else None,
            "chunk_id": chunk_id if isinstance(chunk_id, str)
            else (full_id.split(":", 1)[1] if ":" in full_id else ""),
            "heading": meta.get("heading"),
            "page": _int_or_none(meta.get("page")),
            "section_type": meta.get("section_type"),
            "text": meta.get("text") if isinstance(meta.get("text"), str) else "",
            "score": float(score) if isinstance(score, int | float) else 0.0,
        })
        if len(results) >= k:
            break

    return results


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
