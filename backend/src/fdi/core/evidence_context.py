"""The pure reads behind Evidence context (S4): a page's text, a note's text,
a table's full grid and one cell, and the FOG lookups the structural path
follows (statement -> table, table -> note, line item -> REFERENCES -> note).

Every function reads already-loaded artifact bodies (`ocr`, `structure`,
`fog_objects`, `chunks`) as plain Mappings and is total: malformed input
degrades to None / [] / "" -- an inspect of a damaged artifact says "nothing
there", it never raises. No I/O (`fdi.core` law).
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence

from fdi.core.table_render import render_table_markdown

# A page or note is returned whole up to this many characters: enough for any
# real annual-report page or note (the largest TCS note measured ~18k chars),
# small enough that one inspect cannot ship a megabyte to a model.
MAX_CONTEXT_CHARS = 40_000


def _list(value: object) -> list[object]:
    return value if isinstance(value, list) else []


def _map(value: object) -> Mapping[str, object]:
    return value if isinstance(value, Mapping) else {}


def _int(value: object) -> int | None:
    return value if isinstance(value, int) and not isinstance(value, bool) else None


def clip(text: str, limit: int = MAX_CONTEXT_CHARS) -> str:
    return text if len(text) <= limit else text[:limit] + "\n[... truncated]"


# ---------------------------------------------------------------------------
# OCR: pages and offset windows
# ---------------------------------------------------------------------------


def page_numbers(ocr: Mapping[str, object]) -> list[int]:
    return sorted(
        n for p in _list(ocr.get("pages")) if (n := _int(_map(p).get("page_number"))) is not None
    )


def page_text(ocr: Mapping[str, object], page: int) -> str | None:
    """The text of `page` (1-based), sliced from the OCR `content` by the
    page's own spans. None when the page does not exist."""
    content = ocr.get("content")
    if not isinstance(content, str):
        return None
    for p in _list(ocr.get("pages")):
        pm = _map(p)
        if _int(pm.get("page_number")) != page:
            continue
        parts: list[str] = []
        for span in _list(pm.get("spans")):
            sm = _map(span)
            offset, length = _int(sm.get("offset")), _int(sm.get("length"))
            if offset is None or length is None or offset < 0 or length < 0:
                continue
            parts.append(content[offset:offset + length])
        return "".join(parts)
    return None


def window_text(ocr: Mapping[str, object], start: object, end: object) -> str:
    """OCR `content[start:end]` ("" on malformed bounds)."""
    content = ocr.get("content")
    s, e = _int(start), _int(end)
    if not isinstance(content, str) or s is None or e is None or not 0 <= s <= e:
        return ""
    return content[s:e]


# ---------------------------------------------------------------------------
# structure: tables, grids, cells
# ---------------------------------------------------------------------------


def table_at(structure: Mapping[str, object], index: int) -> Mapping[str, object] | None:
    tables = _list(structure.get("tables"))
    if not 0 <= index < len(tables):
        return None
    table = tables[index]
    return table if isinstance(table, Mapping) else None


def table_count(structure: Mapping[str, object]) -> int:
    return len(_list(structure.get("tables")))


def table_page(table: Mapping[str, object]) -> int | None:
    for region in _list(table.get("bounding_regions")):
        page = _int(_map(region).get("page_number"))
        if page is not None:
            return page
    return None


def table_regions(table: Mapping[str, object]) -> tuple[Mapping[str, object], ...]:
    return tuple(r for r in _list(table.get("bounding_regions")) if isinstance(r, Mapping))


def table_caption(table: Mapping[str, object], index: int) -> str:
    caption = _map(table.get("caption")).get("content")
    return caption if isinstance(caption, str) and caption.strip() else f"Table {index}"


def table_markdown(table: Mapping[str, object], *, identity: str = "") -> str:
    """The whole table as markdown (headers repaired, matrices transposed --
    the extractor's own rendering, so research and extraction read tables
    identically)."""
    return render_table_markdown(_map(table.get("grid")), identity=identity)


def grid_rows(table: Mapping[str, object]) -> list[list[str]]:
    """The raw grid (header rows included) as strings -- the coordinate
    space a `table_cell` Evidence id addresses."""
    rows: list[list[str]] = []
    for row in _list(_map(table.get("grid")).get("grid")):
        rows.append([c if isinstance(c, str) else "" for c in _list(row)])
    return rows


def grid_body(table: Mapping[str, object]) -> dict[str, object]:
    grid = _map(table.get("grid"))
    headers = [h if isinstance(h, str) else "" for h in _list(grid.get("column_headers"))]
    header_rows = _int(grid.get("header_row_count")) or 0
    return {"column_headers": headers, "header_row_count": header_rows, "rows": grid_rows(table)}


def cell(table: Mapping[str, object], row: int, col: int) -> dict[str, object] | None:
    """One cell with the row label and column header a reader needs to
    interpret it. None when the coordinates fall outside the grid."""
    rows = grid_rows(table)
    if not 0 <= row < len(rows) or not 0 <= col < len(rows[row]):
        return None
    body = grid_body(table)
    headers = body["column_headers"]
    assert isinstance(headers, list)
    label = next((c for c in rows[row] if c.strip() and not _numeric(c)), "")
    return {
        "row": row,
        "col": col,
        "value": rows[row][col],
        "row_label": label,
        "column_header": headers[col] if col < len(headers) else "",
    }


def _numeric(text: str) -> bool:
    stripped = text.replace(",", "").replace("(", "").replace(")", "").replace("-", "").strip()
    return stripped.replace(".", "", 1).isdigit()


# ---------------------------------------------------------------------------
# chunks: the full text of a section
# ---------------------------------------------------------------------------


def chunk_record(chunks: Mapping[str, object], chunk_id: str) -> Mapping[str, object] | None:
    for c in _list(chunks.get("chunks")):
        if isinstance(c, Mapping) and c.get("chunk_id") == chunk_id:
            return c
    return None


# ---------------------------------------------------------------------------
# FOG: statements, notes, line items, REFERENCES
# ---------------------------------------------------------------------------


def _region(fog: Mapping[str, object], basis: str) -> Mapping[str, object]:
    return _map(_map(fog.get("bases")).get(basis))


def fog_bases(fog: Mapping[str, object]) -> list[str]:
    return sorted(str(b) for b in _map(fog.get("bases")))


def statements(fog: Mapping[str, object], basis: str) -> list[Mapping[str, object]]:
    return [s for s in _list(_region(fog, basis).get("statements")) if isinstance(s, Mapping)]


def notes(fog: Mapping[str, object], basis: str) -> list[Mapping[str, object]]:
    return [n for n in _list(_region(fog, basis).get("notes")) if isinstance(n, Mapping)]


def find_notes(
    fog: Mapping[str, object], basis: str, *, ref: str | None = None, title: str | None = None
) -> list[Mapping[str, object]]:
    """Notes matching a printed `ref` exactly and/or whose title contains
    `title` (case-insensitive). Neither given -> []."""
    if ref is None and title is None:
        return []
    needle = title.casefold() if title is not None else None
    out = []
    for n in notes(fog, basis):
        if ref is not None and str(n.get("ref")) != ref:
            continue
        if needle is not None and needle not in str(n.get("title", "")).casefold():
            continue
        out.append(n)
    return out


def note_by_id(
    fog: Mapping[str, object], basis: str, note_id: str
) -> Mapping[str, object] | None:
    return next((n for n in notes(fog, basis) if n.get("id") == note_id), None)


def notes_for_table(fog: Mapping[str, object], table_index: int) -> list[tuple[str, str]]:
    """(basis, note id) for every note whose window holds table `table_index`."""
    out: list[tuple[str, str]] = []
    for basis in fog_bases(fog):
        for n in notes(fog, basis):
            if table_index in _list(n.get("table_indices")) and isinstance(n.get("id"), str):
                out.append((basis, str(n["id"])))
    return out


def line_items(fog: Mapping[str, object], basis: str) -> list[Mapping[str, object]]:
    return [i for i in _list(_region(fog, basis).get("line_items")) if isinstance(i, Mapping)]


def line_item(
    fog: Mapping[str, object], basis: str, item_id: str
) -> Mapping[str, object] | None:
    return next((i for i in line_items(fog, basis) if i.get("id") == item_id), None)


def references_from(fog: Mapping[str, object], basis: str, source_id: str) -> list[str]:
    """The FOG node ids a node points at through REFERENCES edges (a
    statement line -> the note it cites). In-text references (Epic #14) will
    arrive as more edges of this same type and need no change here."""
    return sorted(
        str(e["target"])
        for e in _list(_region(fog, basis).get("edges"))
        if isinstance(e, Mapping)
        and e.get("type") == "REFERENCES"
        and e.get("source") == source_id
        and isinstance(e.get("target"), str)
    )


def parent_node(
    fog: Mapping[str, object], basis: str, parent_id: str
) -> Mapping[str, object] | None:
    """The statement or note a line item belongs to."""
    for node in (*statements(fog, basis), *notes(fog, basis)):
        if node.get("id") == parent_id:
            return node
    return None


def items_by_ids(items: Sequence[Mapping[str, object]]) -> dict[str, Mapping[str, object]]:
    return {str(i["id"]): i for i in items if isinstance(i.get("id"), str)}
