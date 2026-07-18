"""Table-grid canonicalization: expand spanning cells into a rectangular grid
and resolve nested/multi-row column headers to ONE joined-path convention.
Pure -- no I/O. (SP-7c; research: "canonicalize to ONE convention before use".)"""

from collections.abc import Mapping
from typing import cast

_HEADER_KIND = "columnHeader"


def _cell_int(cell: Mapping[str, object], key: str, default: int) -> int:
    value = cell.get(key)
    return int(cast(int, value)) if value is not None else default


def _region_page(regions: list[dict[str, object]]) -> int | None:
    if not regions:
        return None
    page = regions[0].get("page_number")
    return page if isinstance(page, int) else None


def canonicalize_table(table: Mapping[str, object]) -> dict[str, object]:
    """Expand a table's spanning cells into a full rectangular grid and
    resolve its leading column-header rows to a single joined-path convention.

    Args:
        table: a normalized table mapping (`row_count`, `column_count`,
            `cells: [{row_index, column_index, row_span, column_span, kind,
            content}]` -- the SP-7a/7b enrichment shape).

    Returns:
        `{"n_rows", "n_cols", "header_row_count", "column_headers", "grid",
        "cell_regions", "cell_pages"}`. `grid` is the `n_rows` x `n_cols`
        matrix of cell content, spanning cells repeated into every cell they
        cover. `column_headers[c]` is the " / "-joined header path for column
        `c`, skipping blank cells and collapsing an entry equal to the
        immediately preceding one (a row-spanning header cell repeats its
        content into every header row it covers). `cell_regions` is the
        parallel `n_rows` x `n_cols` matrix of each cell's normalized
        `bounding_regions` (`[]` if absent), expanded across spans exactly
        like `grid`; `cell_pages[r][c]` is `cell_regions[r][c][0]
        ["page_number"]` (or `None`), a convenience matrix mirroring
        `node_provenance`'s own `page` + `bounding_regions` shape.
    """
    n_rows = _cell_int(table, "row_count", 0)
    n_cols = _cell_int(table, "column_count", 0)
    cells = cast("list[Mapping[str, object]]", table.get("cells") or [])

    grid: list[list[str]] = [["" for _ in range(n_cols)] for _ in range(n_rows)]
    kinds: list[list[object]] = [[None for _ in range(n_cols)] for _ in range(n_rows)]
    cell_regions: list[list[list[dict[str, object]]]] = [
        [[] for _ in range(n_cols)] for _ in range(n_rows)
    ]

    for cell in cells:
        row0 = _cell_int(cell, "row_index", 0)
        col0 = _cell_int(cell, "column_index", 0)
        row_span = _cell_int(cell, "row_span", 1)
        col_span = _cell_int(cell, "column_span", 1)
        content = cast("str | None", cell.get("content")) or ""
        kind = cell.get("kind")
        regions_raw = cell.get("bounding_regions")
        regions: list[dict[str, object]] = (
            [dict(r) for r in regions_raw if isinstance(r, Mapping)]
            if isinstance(regions_raw, list)
            else []
        )
        for r in range(row0, min(row0 + row_span, n_rows)):
            for c in range(col0, min(col0 + col_span, n_cols)):
                grid[r][c] = content
                kinds[r][c] = kind
                cell_regions[r][c] = regions

    header_row_count = 0
    for r in range(n_rows):
        if n_cols == 0 or any(kinds[r][c] != _HEADER_KIND for c in range(n_cols)):
            break
        header_row_count += 1

    column_headers: list[str] = []
    for c in range(n_cols):
        parts: list[str] = []
        for r in range(header_row_count):
            value = grid[r][c]
            if value and (not parts or parts[-1] != value):
                parts.append(value)
        column_headers.append(" / ".join(parts))

    cell_pages: list[list[int | None]] = [
        [_region_page(regions) for regions in row] for row in cell_regions
    ]

    return {
        "n_rows": n_rows,
        "n_cols": n_cols,
        "header_row_count": header_row_count,
        "column_headers": column_headers,
        "grid": grid,
        "cell_regions": cell_regions,
        "cell_pages": cell_pages,
    }
