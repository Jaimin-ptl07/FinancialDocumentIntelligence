from fdi.core.table_render import render_table_markdown, repair_headers


def _grid(
    *,
    n_rows: int,
    n_cols: int,
    header_row_count: int,
    column_headers: list[str],
    grid: list[list[str]],
) -> dict[str, object]:
    return {
        "n_rows": n_rows,
        "n_cols": n_cols,
        "header_row_count": header_row_count,
        "column_headers": column_headers,
        "grid": grid,
        "cell_regions": [],
        "cell_pages": [],
    }


# ---------------------------------------------------------------------------
# repair_headers
# ---------------------------------------------------------------------------


def test_repair_headers_leaves_well_formed_grid_unchanged() -> None:
    g = _grid(
        n_rows=2,
        n_cols=3,
        header_row_count=1,
        column_headers=["Particulars", "As at 31-03-2024", "As at 31-03-2023"],
        grid=[
            ["Particulars", "As at 31-03-2024", "As at 31-03-2023"],
            ["Total assets", "16,427.37", "14,213.08"],
        ],
    )
    out = repair_headers(g)
    assert out["column_headers"] == g["column_headers"]
    assert out is not g


def test_repair_headers_promotes_date_first_row_when_header_row_count_zero() -> None:
    g = _grid(
        n_rows=2,
        n_cols=3,
        header_row_count=0,
        column_headers=["", "", ""],
        grid=[
            ["Particulars", "31-03-2024", "31-03-2023"],
            ["Total assets", "16,427.37", "14,213.08"],
        ],
    )
    out = repair_headers(g)
    headers = out["column_headers"]
    assert isinstance(headers, list)
    assert headers[0] == ""
    assert headers[1] == "31-03-2024"
    assert headers[2] == "31-03-2023"


def test_repair_headers_promotes_when_headers_collapse_to_duplicate_details() -> None:
    g = _grid(
        n_rows=2,
        n_cols=3,
        header_row_count=1,
        column_headers=["Details", "Details", "Details"],
        grid=[
            ["Details", "Details", "Details"],
            ["Total assets", "FY24", "FY23"],
        ],
    )
    out = repair_headers(g)
    headers = out["column_headers"]
    assert isinstance(headers, list)
    assert headers[1] == "FY24"
    assert headers[2] == "FY23"


def test_repair_headers_positional_fallback_when_no_period_pattern() -> None:
    g = _grid(
        n_rows=2,
        n_cols=3,
        header_row_count=1,
        column_headers=["Details", "Details", "Details"],
        grid=[
            ["Details", "Details", "Details"],
            ["Total assets", "16,427.37", "14,213.08"],
        ],
    )
    out = repair_headers(g)
    headers = out["column_headers"]
    assert isinstance(headers, list)
    assert headers[0] == "Details"
    assert headers[1] == "Column 1 (most recent)"
    assert headers[2] == "Column 2 (prior)"


def test_repair_headers_positional_fallback_column_3_and_beyond() -> None:
    g = _grid(
        n_rows=2,
        n_cols=4,
        header_row_count=1,
        column_headers=["Details", "Details", "Details", "Details"],
        grid=[
            ["Details", "Details", "Details", "Details"],
            ["Total assets", "1", "2", "3"],
        ],
    )
    out = repair_headers(g)
    headers = out["column_headers"]
    assert isinstance(headers, list)
    assert headers[3] == "Column 3"


def test_repair_headers_on_malformed_input_missing_keys_does_not_raise() -> None:
    out = repair_headers({})
    assert isinstance(out, dict)


def test_repair_headers_on_non_list_grid_does_not_raise() -> None:
    g: dict[str, object] = {
        "n_rows": 2,
        "n_cols": 2,
        "header_row_count": 0,
        "column_headers": "not-a-list",
        "grid": "not-a-list-either",
    }
    out = repair_headers(g)
    assert isinstance(out, dict)


def test_repair_headers_on_ragged_rows_does_not_raise() -> None:
    g: dict[str, object] = {
        "n_rows": 2,
        "n_cols": 3,
        "header_row_count": 0,
        "column_headers": ["", "", ""],
        "grid": [["A", "B"], ["1", "2", "3"]],
    }
    out = repair_headers(g)
    assert isinstance(out, dict)


def test_repair_headers_row_label_column_stays_as_is() -> None:
    g = _grid(
        n_rows=2,
        n_cols=2,
        header_row_count=0,
        column_headers=["", ""],
        grid=[
            ["Particulars", "31-03-2024"],
            ["Total assets", "16,427.37"],
        ],
    )
    out = repair_headers(g)
    headers = out["column_headers"]
    assert isinstance(headers, list)
    assert headers[0] == ""


# ---------------------------------------------------------------------------
# render_table_markdown
# ---------------------------------------------------------------------------


def test_render_table_markdown_produces_header_separator_and_data_rows_with_title() -> None:
    g = _grid(
        n_rows=2,
        n_cols=3,
        header_row_count=1,
        column_headers=["Particulars", "As at 31-03-2024", "As at 31-03-2023"],
        grid=[
            ["Particulars", "As at 31-03-2024", "As at 31-03-2023"],
            ["Total assets", "16,427.37", "14,213.08"],
        ],
    )
    out = render_table_markdown(
        g, identity="Balance Sheet -- Standalone -- page 91", units="Rs in Lakhs"
    )
    lines = out.splitlines()
    assert lines[0] == "Table: Balance Sheet -- Standalone -- page 91 -- Rs in Lakhs"
    assert lines[1] == ""
    assert lines[2] == "| Particulars | As at 31-03-2024 | As at 31-03-2023 |"
    assert lines[3] == "| --- | --- | --- |"
    assert lines[4] == "| Total assets | 16,427.37 | 14,213.08 |"


def test_render_table_markdown_no_identity_no_title_line() -> None:
    g = _grid(
        n_rows=2,
        n_cols=2,
        header_row_count=1,
        column_headers=["A", "B"],
        grid=[["A", "B"], ["1", "2"]],
    )
    out = render_table_markdown(g)
    assert not out.startswith("Table:")
    assert "| A | B |" in out


def test_render_table_markdown_uses_real_headers_after_repair_on_collapsed_grid() -> None:
    g = _grid(
        n_rows=2,
        n_cols=3,
        header_row_count=1,
        column_headers=["Details", "Details", "Details"],
        grid=[
            ["Details", "Details", "Details"],
            ["Total assets", "FY24", "FY23"],
        ],
    )
    out = render_table_markdown(g)
    assert "Details, Details" not in out
    assert "FY24" in out
    assert "FY23" in out


def test_render_table_markdown_escapes_pipe_in_cell() -> None:
    g = _grid(
        n_rows=2,
        n_cols=2,
        header_row_count=1,
        column_headers=["A", "B"],
        grid=[["A", "B"], ["Debt | Equity", "2"]],
    )
    out = render_table_markdown(g)
    assert "Debt \\| Equity" in out


def test_render_table_markdown_empty_grid_returns_empty_string() -> None:
    out = render_table_markdown({})
    assert out == ""


def test_render_table_markdown_empty_grid_with_identity_returns_title_only() -> None:
    out = render_table_markdown({}, identity="Balance Sheet")
    assert out == "Table: Balance Sheet"


def test_render_table_markdown_never_raises_on_malformed_input() -> None:
    out = render_table_markdown({"grid": "nope", "column_headers": 5})
    assert isinstance(out, str)
