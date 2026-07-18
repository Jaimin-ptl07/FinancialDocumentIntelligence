from fdi.core.table_grid import canonicalize_table


def _cell(
    row: int, col: int, content: str, kind: str | None = None,
    row_span: int = 1, col_span: int = 1,
    bounding_regions: list[dict[str, object]] | None = None,
) -> dict[str, object]:
    cell: dict[str, object] = {
        "row_index": row, "column_index": col, "row_span": row_span,
        "column_span": col_span, "kind": kind, "content": content,
    }
    if bounding_regions is not None:
        cell["bounding_regions"] = bounding_regions
    return cell


def test_simple_bordered_table_builds_grid() -> None:
    table = {
        "row_count": 2, "column_count": 3,
        "cells": [
            _cell(0, 0, "A", kind="columnHeader"),
            _cell(0, 1, "B", kind="columnHeader"),
            _cell(0, 2, "C", kind="columnHeader"),
            _cell(1, 0, "1"),
            _cell(1, 1, "2"),
            _cell(1, 2, "3"),
        ],
    }
    out = canonicalize_table(table)
    assert out["n_rows"] == 2 and out["n_cols"] == 3
    assert out["header_row_count"] == 1
    assert out["column_headers"] == ["A", "B", "C"]
    assert out["grid"] == [["A", "B", "C"], ["1", "2", "3"]]


def test_colspan_header_cell_expands_to_two_columns() -> None:
    table = {
        "row_count": 2, "column_count": 3,
        "cells": [
            _cell(0, 0, "Year ended 31 March", kind="columnHeader", col_span=2),
            _cell(0, 2, "Item", kind="columnHeader"),
            _cell(1, 0, "2023"),
            _cell(1, 1, "2022"),
            _cell(1, 2, "Foo"),
        ],
    }
    out = canonicalize_table(table)
    assert out["grid"][0] == ["Year ended 31 March", "Year ended 31 March", "Item"]
    assert out["n_cols"] == 3


def test_nested_two_row_header_joins_paths() -> None:
    table = {
        "row_count": 2, "column_count": 2,
        "cells": [
            _cell(0, 0, "Year ended 31 March", kind="columnHeader", col_span=2),
            _cell(1, 0, "2023", kind="columnHeader"),
            _cell(1, 1, "2022", kind="columnHeader"),
        ],
    }
    out = canonicalize_table(table)
    assert out["header_row_count"] == 2
    assert out["column_headers"] == [
        "Year ended 31 March / 2023",
        "Year ended 31 March / 2022",
    ]


def test_missing_and_empty_cells_become_blank_in_grid() -> None:
    table = {
        "row_count": 2, "column_count": 2,
        "cells": [
            _cell(0, 0, "H1", kind="columnHeader"),
            _cell(0, 1, "", kind="columnHeader"),  # explicit empty content
            _cell(1, 0, "x"),
            # (1, 1) has no cell at all -- a hole in Azure's cell list
        ],
    }
    out = canonicalize_table(table)
    assert out["grid"] == [["H1", ""], ["x", ""]]


def test_rowspan_cell_fills_down() -> None:
    table = {
        "row_count": 2, "column_count": 2,
        "cells": [
            _cell(0, 0, "Total", row_span=2),
            _cell(0, 1, "X"),
            _cell(1, 1, "Y"),
        ],
    }
    out = canonicalize_table(table)
    assert out["grid"] == [["Total", "X"], ["Total", "Y"]]


def test_pure_and_deterministic() -> None:
    table = {
        "row_count": 1, "column_count": 1,
        "cells": [_cell(0, 0, "X", kind="columnHeader")],
    }
    assert canonicalize_table(table) == canonicalize_table(table)


def test_cell_with_bounding_regions_populates_cell_regions_and_cell_pages() -> None:
    regions = [{"page_number": 5, "polygon": [0, 0, 1, 0, 1, 1, 0, 1]}]
    table = {
        "row_count": 1, "column_count": 1,
        "cells": [_cell(0, 0, "X", bounding_regions=regions)],
    }
    out = canonicalize_table(table)
    assert out["cell_regions"] == [[regions]]
    assert out["cell_pages"] == [[5]]


def test_rowspan_cell_regions_repeat_into_every_row_it_covers() -> None:
    regions = [{"page_number": 3, "polygon": []}]
    table = {
        "row_count": 2, "column_count": 2,
        "cells": [
            _cell(0, 0, "Total", row_span=2, bounding_regions=regions),
            _cell(0, 1, "X"),
            _cell(1, 1, "Y"),
        ],
    }
    out = canonicalize_table(table)
    assert out["cell_regions"][0][0] == regions
    assert out["cell_regions"][1][0] == regions
    assert out["cell_pages"][0][0] == 3
    assert out["cell_pages"][1][0] == 3


def test_cell_with_no_bounding_regions_and_grid_hole_have_empty_regions_and_none_page() -> None:
    table = {
        "row_count": 2, "column_count": 2,
        "cells": [
            _cell(0, 0, "H1", kind="columnHeader"),
            _cell(0, 1, "", kind="columnHeader"),
            _cell(1, 0, "x"),
            # (1, 1) has no cell at all -- a hole in Azure's cell list
        ],
    }
    out = canonicalize_table(table)
    assert out["cell_regions"] == [[[], []], [[], []]]
    assert out["cell_pages"] == [[None, None], [None, None]]


def test_grid_and_column_headers_unchanged_by_cell_regions_addition() -> None:
    table = {
        "row_count": 2, "column_count": 3,
        "cells": [
            _cell(0, 0, "A", kind="columnHeader", bounding_regions=[{"page_number": 1}]),
            _cell(0, 1, "B", kind="columnHeader"),
            _cell(0, 2, "C", kind="columnHeader"),
            _cell(1, 0, "1"),
            _cell(1, 1, "2"),
            _cell(1, 2, "3"),
        ],
    }
    out = canonicalize_table(table)
    assert out["n_rows"] == 2 and out["n_cols"] == 3
    assert out["header_row_count"] == 1
    assert out["column_headers"] == ["A", "B", "C"]
    assert out["grid"] == [["A", "B", "C"], ["1", "2", "3"]]
