from fdi.core.table_grid import canonicalize_table


def _cell(
    row: int, col: int, content: str, kind: str | None = None,
    row_span: int = 1, col_span: int = 1,
) -> dict[str, object]:
    return {
        "row_index": row, "column_index": col, "row_span": row_span,
        "column_span": col_span, "kind": kind, "content": content,
    }


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
