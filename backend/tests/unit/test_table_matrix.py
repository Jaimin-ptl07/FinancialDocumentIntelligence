"""Transposed matrix schedules (PPE/intangibles) render entity-per-row."""

from fdi.core.table_render import is_matrix_schedule, render_table_markdown, transpose_grid

_PPE = {
    "n_cols": 4,
    "header_row_count": 1,
    "column_headers": ["", "Freehold land", "Buildings", "Plant and equipment"],
    "grid": [
        ["", "Freehold land", "Buildings", "Plant and equipment"],
        ["Cost as at April 1, 2025", "323", "8,629", "979"],
        ["Additions", "-", "63", "50"],
        ["Cost as at March 31, 2026", "323", "8,685", "1,024"],
        ["Net carrying amount as at March 31, 2026", "323", "3,785", "336"],
    ],
}

_NORMAL = {
    "n_cols": 3,
    "header_row_count": 1,
    "column_headers": ["", "As at March 31, 2026", "As at March 31, 2025"],
    "grid": [
        ["", "As at March 31, 2026", "As at March 31, 2025"],
        ["Cash and cash equivalents", "1,833", "2,610"],
        ["Other balances with banks", "2,953", "4,542"],
    ],
}


def test_matrix_schedule_is_detected_by_where_the_period_lives():
    # periods in the ROW labels, entities across the top
    assert is_matrix_schedule(_PPE) is True
    # periods in the HEADERS -- the ordinary shape, must not be flipped
    assert is_matrix_schedule(_NORMAL) is False


def test_transpose_puts_each_entity_on_its_own_row():
    out = transpose_grid(_PPE)

    assert out["transposed"] is True
    assert out["column_headers"][1] == "Cost as at April 1, 2025"
    rows = {r[0]: r for r in out["grid"][1:]}
    # Buildings now reads as a label followed by its own movements
    assert rows["Buildings"] == ["Buildings", "8,629", "63", "8,685", "3,785"]
    assert rows["Plant and equipment"][3] == "1,024"


def test_rendered_markdown_exposes_the_value_next_to_its_entity():
    md = render_table_markdown(_PPE, identity="Note 8(a)")

    assert "| Buildings | 8,629 | 63 | 8,685 | 3,785 |" in md
    # the gross-block figure is adjacent to its label, not 3 columns away
    assert "8,685" in md.split("| Buildings |")[1].split("\n")[0]


def test_ordinary_table_renders_unchanged():
    md = render_table_markdown(_NORMAL)

    assert "| Cash and cash equivalents | 1,833 | 2,610 |" in md
    assert "transposed" not in md


def test_transpose_is_total_on_malformed_input():
    assert transpose_grid({}) == {}
    assert transpose_grid({"n_cols": "x"}) == {"n_cols": "x"}
    assert is_matrix_schedule(None) is False  # type: ignore[arg-type]
    assert is_matrix_schedule({"n_cols": 9, "grid": [None, 3]}) is False


def test_narrow_grid_is_not_flipped_even_with_period_rows():
    narrow = {
        "n_cols": 2, "header_row_count": 1,
        "column_headers": ["", "Total"],
        "grid": [["", "Total"], ["Balance as at March 31, 2026", "100"],
                 ["Balance as at March 31, 2025", "90"]],
    }
    assert is_matrix_schedule(narrow) is False
