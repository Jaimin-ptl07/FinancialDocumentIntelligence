"""Provenance: trace every SP-9 graph node back to its source OCR paragraph's
page and bounding geometry, and resolve a retrieval chunk id to the geometry
behind it. Pure, deterministic; no I/O.

Node resolution is a DIRECT INDEX LOOKUP: a `section`/`paragraph` node carries
its origin OCR paragraph's position (`source_index`, threaded from `unified` ->
`ast` -> `graph` by Phase 2), so `ocr["paragraphs"][source_index]` is the
node's own paragraph -- never a text/label match, never ambiguous.

Chunk resolution (`resolve_chunk_regions`, `resolve_evidence_regions`) is the
read side an answer-highlighting UI needs. It reads `provenance` and
`structure` as plain Mappings -- no import of fdi.core.retrieval/graph/
table_grid, no import of fdi.pipeline.*/fdi.platform.* -- and is explicit
about where the join is lossy; see `resolve_chunk_regions`'s docstring."""

import re
from collections.abc import Mapping, Sequence

from fdi.core.tables import find_field_rows


def _paragraph_regions(
    ocr_paragraphs: list[object], index: object
) -> list[dict[str, object]] | None:
    """`ocr_paragraphs[index]["bounding_regions"]`, or `None` if `index` isn't
    a valid in-range (non-bool) `int`, the paragraph isn't a `Mapping`, or its
    `bounding_regions` is missing/malformed/empty. Never raises."""
    if not isinstance(index, int) or isinstance(index, bool):
        return None
    if index < 0 or index >= len(ocr_paragraphs):
        return None
    para = ocr_paragraphs[index]
    if not isinstance(para, Mapping):
        return None
    regions = para.get("bounding_regions")
    if not isinstance(regions, list) or not regions:
        return None
    resolved = [dict(r) for r in regions if isinstance(r, Mapping)]
    return resolved or None


def _page_of(regions: list[dict[str, object]]) -> int | None:
    """The first region's page_number, if present and an int."""
    if not regions:
        return None
    page = regions[0].get("page_number")
    return page if isinstance(page, int) else None


def _ocr_paragraphs(ocr: Mapping[str, object]) -> list[object]:
    paragraphs_raw = ocr.get("paragraphs")
    return paragraphs_raw if isinstance(paragraphs_raw, list) else []


def _first_paragraph_with_regions(ocr_paragraphs: list[object]) -> dict[str, object] | None:
    """The FIRST ocr paragraph (document order) with usable `bounding_regions`
    -- the `doc` node's fallback when it has no usable `source_index`. A
    positional scan, not a label match, so it's unaffected by the ambiguity
    bug and kept as-is (spec §2 decision 7)."""
    for i in range(len(ocr_paragraphs)):
        regions = _paragraph_regions(ocr_paragraphs, i)
        if regions is not None and _page_of(regions) is not None:
            return {"bounding_regions": regions, "page": _page_of(regions)}
    return None


def build_provenance(graph: Mapping[str, object], ocr: Mapping[str, object]) -> dict[str, object]:
    """Resolve every `graph` node to the page + bounding geometry of the OCR
    paragraph it was built from, via a direct index lookup into
    `ocr["paragraphs"][node["source_index"]]` -- O(1) per node, no text/label
    matching anywhere in this function.

    A node whose `source_index` is `None`, not a (non-bool) `int`, negative,
    out of range, or whose paragraph is not a `Mapping` or has empty/unusable
    `bounding_regions`, is OMITTED from `node_provenance` -- never emitted
    with a null/placeholder page (unchanged conservative policy).

    The `doc` node (`id == "doc"`) is index-first: if its `source_index`
    resolves to a paragraph with usable geometry, use it directly; otherwise
    fall back to the first OCR paragraph (document order) with any non-empty,
    page-bearing `bounding_regions` -- this fallback scan is positional, not a
    label match, so it predates and is unaffected by the ambiguity fix.
    """
    ocr_paragraphs = _ocr_paragraphs(ocr)

    nodes_raw = graph.get("nodes")
    nodes: list[Mapping[str, object]] = []
    if isinstance(nodes_raw, list):
        for entry in nodes_raw:
            if isinstance(entry, Mapping):
                nodes.append(entry)

    node_provenance: list[dict[str, object]] = []
    for node in nodes:
        node_id = node.get("id")
        if not isinstance(node_id, str):
            continue

        regions = _paragraph_regions(ocr_paragraphs, node.get("source_index"))
        page = _page_of(regions) if regions is not None else None

        if node_id == "doc" and page is None:
            fallback = _first_paragraph_with_regions(ocr_paragraphs)
            if fallback is not None:
                node_provenance.append({
                    "node_id": "doc",
                    "page": fallback["page"],
                    "bounding_regions": fallback["bounding_regions"],
                })
            continue

        if page is None or regions is None:
            continue
        node_provenance.append({"node_id": node_id, "page": page, "bounding_regions": regions})

    return {"schema_version": 1, "node_provenance": node_provenance}


_SECTION_RE = re.compile(r"^s(\d+)$")
_TABLE_RE = re.compile(r"^t(\d+)$")
_TABLE_ROW_RE = re.compile(r"^t(\d+)r(\d+)$")


def _region_key(region: Mapping[str, object]) -> tuple[object, ...]:
    """A hashable identity for a bounding region: its page plus its polygon
    coordinates. Used only to deduplicate; never persisted."""
    polygon = region.get("polygon")
    coords = tuple(polygon) if isinstance(polygon, list) else ()
    return (region.get("page_number"), coords)


def _dedup_regions(regions: list[dict[str, object]]) -> list[dict[str, object]]:
    seen: set[tuple[object, ...]] = set()
    out: list[dict[str, object]] = []
    for r in regions:
        key = _region_key(r)
        if key in seen:
            continue
        seen.add(key)
        out.append(r)
    return out


def _regions_list(value: object) -> list[dict[str, object]]:
    if not isinstance(value, list):
        return []
    return [dict(r) for r in value if isinstance(r, Mapping)]


def _node_regions(provenance: Mapping[str, object], node_id: str) -> list[dict[str, object]]:
    entries = provenance.get("node_provenance")
    if not isinstance(entries, list):
        return []
    for entry in entries:
        if not isinstance(entry, Mapping):
            continue
        if entry.get("node_id") != node_id:
            continue
        return _regions_list(entry.get("bounding_regions"))
    return []


def _table_at(structure: Mapping[str, object], index: int) -> Mapping[str, object] | None:
    tables = structure.get("tables")
    if not isinstance(tables, list) or index < 0 or index >= len(tables):
        return None
    table = tables[index]
    return table if isinstance(table, Mapping) else None


def _table_row_regions(
    table: Mapping[str, object], row: int
) -> list[dict[str, object]] | None:
    """Every cell region on grid row `row`, flattened and deduplicated. A
    spanning cell repeats its regions into each cell it covers (see
    core.table_grid.canonicalize_table), so dedup is required, not tidy.

    `None` and `[]` are DIFFERENT answers and the caller depends on it:
    `None` means the row does not exist (no grid, or an out-of-range index --
    a malformed id, which must resolve to nothing at all), while `[]` means
    the row exists but carries no per-cell geometry, which is the case that
    earns the whole-table fallback.
    """
    grid = table.get("grid")
    if not isinstance(grid, Mapping):
        return None
    cell_regions = grid.get("cell_regions")
    if not isinstance(cell_regions, list) or row < 0 or row >= len(cell_regions):
        return None
    row_cells = cell_regions[row]
    if not isinstance(row_cells, list):
        return None
    flat: list[dict[str, object]] = []
    for cell in row_cells:
        flat.extend(_regions_list(cell))
    return _dedup_regions(flat)


def resolve_chunk_regions(
    chunk_id: str,
    provenance: Mapping[str, object],
    structure: Mapping[str, object],
) -> list[dict[str, object]]:
    """The bounding geometry behind ONE `chunks` record, by its chunk_id.

    Three id shapes exist (core.retrieval.build_chunks) and each resolves
    through a different artifact, because the graph does not carry table
    geometry at all:

    * `s{i}` -> the `provenance` artifact's node_provenance entry with the
      SAME id. LOSSY BY CONSTRUCTION: core.graph.build_graph sets the `s{i}`
      node's source_index from the section's `heading_source_index`, so this
      is the HEADING paragraph's box -- while the chunk's text is the heading
      plus every body paragraph. The body paragraphs have their own `s{i}p{j}`
      nodes with their own boxes, but no chunk maps to a paragraph, so there
      is no non-invented way to say which one a value came from. The heading
      box names the right section and the right page and under-covers the
      region; unioning the paragraph nodes to fake a fuller box would claim a
      precision the extraction does not have. A section whose heading has no
      usable geometry is OMITTED from node_provenance entirely (this module's
      conservative policy, unchanged) and therefore resolves to [].

    * `t{i}r{j}` -> `structure["tables"][i]["grid"]["cell_regions"][j]`,
      flattened across the row's cells. Falls back to the whole-table regions
      when the row EXISTS but carries no per-cell geometry (older OCR): a
      page-level answer beats none, and the table box is a true superset of
      the row. An out-of-range row index is a malformed id, not a
      geometryless row, and resolves to [] -- the fallback would otherwise
      paint the whole table for a row that was never there.

    * `t{i}` -> `structure["tables"][i]["bounding_regions"]` (one region per
      source page after a cross-page merge).

    Anything else -- `s{i}p{j}`, `f{k}`, a malformed or hallucinated id --
    resolves to []. Pure and total: no I/O, never raises, whatever the two
    artifacts contain (including being entirely absent).
    """
    row_match = _TABLE_ROW_RE.match(chunk_id)
    if row_match is not None:
        table = _table_at(structure, int(row_match.group(1)))
        if table is None:
            return []
        regions = _table_row_regions(table, int(row_match.group(2)))
        if regions is None:
            return []
        return regions or _dedup_regions(_regions_list(table.get("bounding_regions")))

    table_match = _TABLE_RE.match(chunk_id)
    if table_match is not None:
        table = _table_at(structure, int(table_match.group(1)))
        if table is None:
            return []
        return _dedup_regions(_regions_list(table.get("bounding_regions")))

    if _SECTION_RE.match(chunk_id) is not None:
        return _dedup_regions(_node_regions(provenance, chunk_id))

    return []


def resolve_evidence_regions(
    doc_id: str,
    evidence_ids: Sequence[object],
    provenance: Mapping[str, object],
    structure: Mapping[str, object],
) -> list[dict[str, object]]:
    """Union of `resolve_chunk_regions` over an extracted field's
    evidence_ids, in citation order, deduplicated on (page_number, polygon).

    An evidence_id is `f"{doc_id}:{chunk_id}"` (fdi.pipeline.extractor). A
    bare chunk id with no prefix is accepted too; an id prefixed with a
    DIFFERENT document's id is not this document's geometry and contributes
    nothing. Non-str entries are skipped. Pure, total, never raises."""
    prefix = f"{doc_id}:"
    out: list[dict[str, object]] = []
    for raw in evidence_ids:
        if not isinstance(raw, str) or not raw:
            continue
        if raw.startswith(prefix):
            chunk_id = raw[len(prefix):]
        elif ":" in raw:
            continue
        else:
            chunk_id = raw
        out.extend(resolve_chunk_regions(chunk_id, provenance, structure))
    return _dedup_regions(out)


def _table_grids(structure: Mapping[str, object]) -> list[Mapping[str, object]]:
    tables = structure.get("tables")
    if not isinstance(tables, list):
        return []
    out: list[Mapping[str, object]] = []
    for t in tables:
        if not isinstance(t, Mapping):
            continue
        grid = t.get("grid")
        if isinstance(grid, Mapping):
            out.append(grid)
    return out


def _cell_regions_at(grid: Mapping[str, object], row: int, col: int) -> list[dict[str, object]]:
    cell_regions = grid.get("cell_regions")
    if not isinstance(cell_regions, list) or row < 0 or row >= len(cell_regions):
        return []
    row_cells = cell_regions[row]
    if not isinstance(row_cells, list) or col < 0 or col >= len(row_cells):
        return []
    return _regions_list(row_cells[col])


def resolve_field_regions(
    *,
    keywords: tuple[str, ...],
    structure: Mapping[str, object],
    fallback_current: list[dict[str, object]],
) -> dict[str, list[dict[str, object]]]:
    """Cell-precise geometry for one extracted field, split by period.

    Matches `keywords` (a FieldSpec's own tokens) against every table's
    row-label column via `fdi.core.tables.find_field_rows` (same token-
    subsequence algorithm `resolve_chunk_regions`'s row-id path was built on,
    now reachable without an LLM-cited row id -- see
    docs/superpowers/specs/2026-07-25-cell-precise-bounding-regions-design.md).

    On the FIRST matching row across all tables: current = column 1's cell
    region, prior = column 2's cell region (fdi.core.table_render's existing
    column-1-is-current/column-2-is-prior convention -- a table with fewer
    than 3 columns has no prior column and `prior` stays []).

    On no match anywhere (narrative field, no table row found, malformed
    structure): `current` is `fallback_current` (whatever
    resolve_evidence_regions already resolved -- whole-table or whole-
    section geometry), `prior` is []. Never raises (decision 10): a failed
    cell match degrades to a coarser box, never no box, never a crash.
    """
    for grid in _table_grids(structure):
        matches = find_field_rows(grid, keywords)
        if not matches:
            continue
        row_obj = matches[0].get("row")
        if not isinstance(row_obj, int):
            continue
        current = _dedup_regions(_cell_regions_at(grid, row_obj, 1))
        prior = _dedup_regions(_cell_regions_at(grid, row_obj, 2))
        if current or prior:
            return {"current": current or fallback_current, "prior": prior}

    return {"current": fallback_current, "prior": []}
