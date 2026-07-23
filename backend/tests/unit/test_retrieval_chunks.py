from fdi.core.retrieval import build_chunks


def _ast(sections: list[object]) -> dict[str, object]:
    return {"schema_version": 1, "title": "T", "sections": sections}


def _grid(
    rows: list[list[str]], column_headers: list[str], header_row_count: int
) -> dict[str, object]:
    return {
        "n_rows": len(rows),
        "n_cols": len(column_headers),
        "header_row_count": header_row_count,
        "column_headers": column_headers,
        "grid": rows,
    }


def test_build_chunks_joins_text_type_page() -> None:
    ast = _ast([
        {"heading": "Balance Sheet", "paragraphs": ["Assets 100", "Liab 40"]},
        {"heading": "Notes", "paragraphs": ["Note 1"]},
    ])
    semantic = {"sections": [{"type": "BALANCE_SHEET"}, {"type": "NOTES"}]}
    provenance = {"node_provenance": [
        {"node_id": "s0", "page": 3}, {"node_id": "s1", "page": 7},
    ]}
    out = build_chunks(ast, semantic, provenance, {}, {})
    assert out["schema_version"] == 1
    assert out["chunks"] == [
        {"chunk_id": "s0", "heading": "Balance Sheet",
         "text": "Balance Sheet\nAssets 100\nLiab 40",
         "section_type": "BALANCE_SHEET", "page": 3, "para_count": 2},
        {"chunk_id": "s1", "heading": "Notes",
         "text": "Notes\nNote 1", "section_type": "NOTES", "page": 7, "para_count": 1},
    ]


def test_text_matches_embedding_construction() -> None:
    # byte-identical to EmbeddingStage: heading + "\n" + "\n".join(paragraphs)
    heading, paragraphs = "H", ["a", "b"]
    ast = _ast([{"heading": heading, "paragraphs": paragraphs}])
    out = build_chunks(ast, {}, {}, {}, {})
    assert out["chunks"][0]["text"] == heading + "\n" + "\n".join(paragraphs)


def test_chunk_id_mirrors_enumerate_skip() -> None:
    # index 1 is a non-dict -> skipped, consumes the index; next real section is s2
    ast = _ast([
        {"heading": "A", "paragraphs": []},
        "not-a-dict",
        {"heading": "C", "paragraphs": []},
    ])
    out = build_chunks(ast, {}, {}, {}, {})
    assert [c["chunk_id"] for c in out["chunks"]] == ["s0", "s2"]


def test_section_type_degrades_to_none() -> None:
    ast = _ast([{"heading": "A", "paragraphs": []}, {"heading": "B", "paragraphs": []}])
    semantic = {"sections": [{"type": 123}]}  # non-str type, and only one entry
    out = build_chunks(ast, semantic, {}, {}, {})
    assert [c["section_type"] for c in out["chunks"]] == [None, None]


def test_page_degrades_to_none_when_provenance_missing() -> None:
    ast = _ast([{"heading": "A", "paragraphs": []}, {"heading": "B", "paragraphs": []}])
    provenance = {"node_provenance": [{"node_id": "s0", "page": 5}]}  # s1 absent
    out = build_chunks(ast, {}, provenance, {}, {})
    assert [c["page"] for c in out["chunks"]] == [5, None]


def test_empty_inputs() -> None:
    assert build_chunks({}, {}, {}, {}, {}) == {"schema_version": 1, "chunks": []}


def test_none_heading_kept_as_none_but_text_uses_empty() -> None:
    ast = _ast([{"heading": None, "paragraphs": ["x"]}])
    out = build_chunks(ast, {}, {}, {}, {})
    assert out["chunks"][0]["heading"] is None
    assert out["chunks"][0]["text"] == "\nx"


def test_structure_without_tables_key_emits_only_section_chunks() -> None:
    ast = _ast([{"heading": "A", "paragraphs": ["x"]}])
    out = build_chunks(ast, {}, {}, {"blocks": []}, {})
    assert [c["chunk_id"] for c in out["chunks"]] == ["s0"]


def test_table_card_emitted_once_per_table_with_identity_columns_rows() -> None:
    """TR-1 P2: ONE card per table (`t{i}`) -- no `t{i}r{j}` row chunks at
    all. The card's text is the identity line (statement type / basis / page)
    + resolved column headers + data-row labels, no values."""
    table = {
        "caption": {"content": "Balance Sheet"},
        "bounding_regions": [{"page_number": 5, "polygon": []}],
        "grid": _grid(
            rows=[
                ["Particulars", "As at 31-03-2024", "As at 31-03-2023"],
                ["Property, plant and equipment", "2,126.88", "2,016.80"],
                ["Total assets", "16,427.37", "14,213.08"],
            ],
            column_headers=["Particulars", "As at 31-03-2024", "As at 31-03-2023"],
            header_row_count=1,
        ),
    }
    structure = {"tables": [table]}
    out = build_chunks({}, {}, {}, structure, {})

    # zero t0r* row chunks survive
    assert not any(
        isinstance(c["chunk_id"], str) and c["chunk_id"].startswith("t0r")
        for c in out["chunks"]
    )

    card = next(c for c in out["chunks"] if c["chunk_id"] == "t0")
    assert card == {
        "chunk_id": "t0",
        "heading": "Balance Sheet",
        "text": (
            "[Balance Sheet | BS | basis=unknown | page 5]\n"
            "Columns: As at 31-03-2024 | As at 31-03-2023\n"
            "Rows: Property, plant and equipment; Total assets"
        ),
        "section_type": None,
        "page": 5,
        "para_count": None,
    }


def test_table_card_basis_reflects_page_basis_consolidated() -> None:
    table = {
        "caption": {"content": "Consolidated Balance Sheet"},
        "bounding_regions": [{"page_number": 91, "polygon": []}],
        "grid": _grid(
            rows=[["Particulars", "FY24"], ["Total assets", "500"]],
            column_headers=["Particulars", "FY24"],
            header_row_count=1,
        ),
    }
    structure = {"tables": [table]}
    basis = {
        "schema_version": 1,
        "regions": [{"start_page": 90, "end_page": 95, "basis": "consolidated"}],
        "default_basis": "unknown",
    }
    out = build_chunks({}, {}, {}, structure, basis)
    card = next(c for c in out["chunks"] if c["chunk_id"] == "t0")
    assert "basis=consolidated" in card["text"]
    assert card["text"].startswith("[Consolidated Balance Sheet | BS | basis=consolidated")


def test_table_card_basis_unknown_when_no_region_covers_the_page() -> None:
    table = {
        "caption": {"content": "Balance Sheet"},
        "bounding_regions": [{"page_number": 5, "polygon": []}],
        "grid": _grid(
            rows=[["Particulars", "FY24"], ["Total assets", "500"]],
            column_headers=["Particulars", "FY24"],
            header_row_count=1,
        ),
    }
    structure = {"tables": [table]}
    # a region exists, but it does not cover page 5 -> falls back to default_basis
    basis = {
        "schema_version": 1,
        "regions": [{"start_page": 90, "end_page": 95, "basis": "consolidated"}],
        "default_basis": "unknown",
    }
    out = build_chunks({}, {}, {}, structure, basis)
    card = next(c for c in out["chunks"] if c["chunk_id"] == "t0")
    assert "basis=unknown" in card["text"]


def test_table_card_statement_type_note_units_best_effort_detection() -> None:
    table = {
        "caption": {"content": "Note 24 Statement of Profit and Loss (Rs in Crore)"},
        "bounding_regions": [{"page_number": 12, "polygon": []}],
        "grid": _grid(
            rows=[["Particulars", "FY24"], ["Revenue", "1,000"]],
            column_headers=["Particulars", "FY24"],
            header_row_count=1,
        ),
    }
    structure = {"tables": [table]}
    out = build_chunks({}, {}, {}, structure, {})
    card = next(c for c in out["chunks"] if c["chunk_id"] == "t0")
    assert card["text"] == (
        "[Note 24 Statement of Profit and Loss (Rs in Crore) | PL | "
        "basis=unknown | note 24 | Rs Crore | page 12]\n"
        "Columns: FY24\n"
        "Rows: Revenue"
    )


def test_table_card_cash_flow_and_lakh_units_detected() -> None:
    table = {
        "caption": {"content": "3. Cash Flow Statement (Rs in Lakhs)"},
        "grid": _grid(
            rows=[["Particulars", "FY24"], ["Net cash", "10"]],
            column_headers=["Particulars", "FY24"],
            header_row_count=1,
        ),
    }
    structure = {"tables": [table]}
    out = build_chunks({}, {}, {}, structure, {})
    card = next(c for c in out["chunks"] if c["chunk_id"] == "t0")
    assert "CF" in card["text"]
    assert "note 3" in card["text"]
    assert "Rs Lakhs" in card["text"]


def test_table_card_statement_type_note_units_default_when_undetectable() -> None:
    table = {
        "caption": {"content": "Property, Plant and Equipment"},
        "grid": _grid(
            rows=[["Particulars", "FY24"], ["Gross block", "10"]],
            column_headers=["Particulars", "FY24"],
            header_row_count=1,
        ),
    }
    structure = {"tables": [table]}
    out = build_chunks({}, {}, {}, structure, {})
    card = next(c for c in out["chunks"] if c["chunk_id"] == "t0")
    assert card["text"] == (
        "[Property, Plant and Equipment | unknown | basis=unknown | page None]\n"
        "Columns: FY24\n"
        "Rows: Gross block"
    )


def test_table_context_falls_back_to_caption_then_table_index() -> None:
    table_no_caption = {
        "grid": _grid(
            rows=[["Item", "Value"], ["Cash", "1"]],
            column_headers=["Item", "Value"],
            header_row_count=1,
        ),
    }
    structure = {"tables": [{}, table_no_caption]}
    out = build_chunks({}, {}, {}, structure, {})

    card = next(c for c in out["chunks"] if c["chunk_id"] == "t1")
    assert card["heading"] == "Table 1"
    assert card["text"] == (
        "[Table 1 | unknown | basis=unknown | page None]\n"
        "Columns: Value\n"
        "Rows: Cash"
    )


def test_table_page_none_when_no_bounding_regions() -> None:
    table = {
        "grid": _grid(
            rows=[["Item", "Value"], ["Cash", "1"]],
            column_headers=["Item", "Value"],
            header_row_count=1,
        ),
    }
    structure = {"tables": [table]}
    out = build_chunks({}, {}, {}, structure, {})
    card = next(c for c in out["chunks"] if c["chunk_id"] == "t0")
    assert card["page"] is None


def test_dedup_removes_exact_duplicate_table_cards() -> None:
    table = {
        "caption": {"content": "Same Caption"},
        "grid": _grid(
            rows=[["Item", "Value"], ["Cash", "1"]],
            column_headers=["Item", "Value"],
            header_row_count=1,
        ),
    }
    structure = {"tables": [table, dict(table)]}
    out = build_chunks({}, {}, {}, structure, {})
    ids = [c["chunk_id"] for c in out["chunks"]]
    # first table's card survives; second table's is a byte-identical
    # duplicate (same caption, grid, page, basis) and is dropped, WITHOUT
    # renumbering the survivor
    assert ids == ["t0"]


def test_dedup_collapses_section_and_table_card_with_identical_text() -> None:
    # Crafted so the SECTION chunk's text is byte-identical to the table
    # card's text -- proves the dedup pass runs over the combined
    # section+table list, not just within one group. The section chunk,
    # emitted first, survives; the later-emitted table card ("t0") is dropped.
    ast = _ast([{
        "heading": "[Table 0 | unknown | basis=unknown | page None]",
        "paragraphs": ["Columns: ", "Rows: Cash"],
    }])
    table = {
        "grid": _grid(
            rows=[["Item"], ["Cash"]],
            column_headers=["Item"],
            header_row_count=1,
        ),
    }
    structure = {"tables": [table]}
    out = build_chunks(ast, {}, {}, structure, {})
    ids = [c["chunk_id"] for c in out["chunks"]]
    assert ids == ["s0"]


# --- L1: label column chosen by text density ------------------------------

def test_label_column_index_picks_descriptive_over_leading_index() -> None:
    from fdi.core.retrieval import _label_column_index
    rows = [
        ["Particulars", "Particulars", "Note No.", "31-03-2024"],  # header
        ["I.", "Revenue from operations", "22", "20676.58"],
        ["II.", "Other income", "23", "248.45"],
        ["", "Total expenses", "", "18795.89"],
    ]
    assert _label_column_index(rows, 1, len(rows), 4) == 1


def test_label_column_index_leftmost_on_tie_and_zero_fallback() -> None:
    from fdi.core.retrieval import _label_column_index
    # all-numeric grid -> no descriptive column -> fall back to 0
    rows = [["A", "B"], ["1", "2"], ["3", "4"]]
    assert _label_column_index(rows, 1, len(rows), 2) == 0
    # two equally-descriptive columns -> leftmost wins
    rows2 = [["H1", "H2"], ["Total assets", "Total equity"], ["Cash held", "Debt owed"]]
    assert _label_column_index(rows2, 1, len(rows2), 2) == 0


def test_table_card_uses_descriptive_label_column() -> None:
    """A P&L-style grid whose leading column is a roman-numeral INDEX must
    take its row labels from the descriptive column, not the index column."""
    table = {
        "caption": {"content": "Statement of Profit and Loss"},
        "bounding_regions": [{"page_number": 92, "polygon": []}],
        "grid": _grid(
            rows=[
                ["Particulars", "Particulars", "Note No.", "31-03-2024", "31-03-2023"],
                ["I.", "Revenue from operations", "22", "20676.58", "15219.54"],
                ["II.", "Other income", "23", "248.45", "265.07"],
                ["", "Total expenses", "", "18795.89", "14211.52"],
            ],
            column_headers=["Particulars", "Particulars", "Note No.", "31-03-2024", "31-03-2023"],
            header_row_count=1,
        ),
    }
    out = build_chunks({}, {}, {}, {"tables": [table]}, {})
    card = next(c for c in out["chunks"] if c["chunk_id"] == "t0")
    assert "Rows: Revenue from operations; Other income; Total expenses" in card["text"]
    assert "Columns: Particulars | Note No. | 31-03-2024 | 31-03-2023" in card["text"]
    assert "I.;" not in card["text"]  # the index column must NOT be the labels
