from fdi.core.retrieval import build_chunks, is_row_chunk_id


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
    out = build_chunks(ast, semantic, provenance, {})
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
    out = build_chunks(ast, {}, {}, {})
    assert out["chunks"][0]["text"] == heading + "\n" + "\n".join(paragraphs)


def test_chunk_id_mirrors_enumerate_skip() -> None:
    # index 1 is a non-dict -> skipped, consumes the index; next real section is s2
    ast = _ast([
        {"heading": "A", "paragraphs": []},
        "not-a-dict",
        {"heading": "C", "paragraphs": []},
    ])
    out = build_chunks(ast, {}, {}, {})
    assert [c["chunk_id"] for c in out["chunks"]] == ["s0", "s2"]


def test_section_type_degrades_to_none() -> None:
    ast = _ast([{"heading": "A", "paragraphs": []}, {"heading": "B", "paragraphs": []}])
    semantic = {"sections": [{"type": 123}]}  # non-str type, and only one entry
    out = build_chunks(ast, semantic, {}, {})
    assert [c["section_type"] for c in out["chunks"]] == [None, None]


def test_page_degrades_to_none_when_provenance_missing() -> None:
    ast = _ast([{"heading": "A", "paragraphs": []}, {"heading": "B", "paragraphs": []}])
    provenance = {"node_provenance": [{"node_id": "s0", "page": 5}]}  # s1 absent
    out = build_chunks(ast, {}, provenance, {})
    assert [c["page"] for c in out["chunks"]] == [5, None]


def test_empty_inputs() -> None:
    assert build_chunks({}, {}, {}, {}) == {"schema_version": 1, "chunks": []}


def test_none_heading_kept_as_none_but_text_uses_empty() -> None:
    ast = _ast([{"heading": None, "paragraphs": ["x"]}])
    out = build_chunks(ast, {}, {}, {})
    assert out["chunks"][0]["heading"] is None
    assert out["chunks"][0]["text"] == "\nx"


def test_structure_without_tables_key_emits_only_section_chunks() -> None:
    ast = _ast([{"heading": "A", "paragraphs": ["x"]}])
    out = build_chunks(ast, {}, {}, {"blocks": []})
    assert [c["chunk_id"] for c in out["chunks"]] == ["s0"]


def test_table_row_chunks_emitted_with_correct_id_text_page() -> None:
    table = {
        "caption": {"content": "Balance Sheet Table"},
        "bounding_regions": [{"page_number": 5, "polygon": []}],
        "grid": _grid(
            rows=[["Item", "Value"], ["Cash", "100"], ["Debt", ""]],
            column_headers=["Item", "Value"],
            header_row_count=1,
        ),
    }
    structure = {"tables": [table]}
    out = build_chunks({}, {}, {}, structure)

    row_chunks = {c["chunk_id"]: c for c in out["chunks"] if c["chunk_id"] in ("t0r1", "t0r2")}
    assert set(row_chunks) == {"t0r1", "t0r2"}

    assert row_chunks["t0r1"] == {
        "chunk_id": "t0r1",
        "heading": "Balance Sheet Table",
        "text": "Balance Sheet Table | Cash: Value=100",
        "section_type": None,
        "page": 5,
        "para_count": None,
    }
    # blank cell ("Debt", "") is omitted from the col=val pair list entirely
    assert row_chunks["t0r2"] == {
        "chunk_id": "t0r2",
        "heading": "Balance Sheet Table",
        "text": "Balance Sheet Table | Debt: ",
        "section_type": None,
        "page": 5,
        "para_count": None,
    }


def test_whole_table_chunk_emitted_per_table() -> None:
    table = {
        "caption": {"content": "Balance Sheet Table"},
        "bounding_regions": [{"page_number": 5, "polygon": []}],
        "grid": _grid(
            rows=[["Item", "Value"], ["Cash", "100"], ["Debt", ""]],
            column_headers=["Item", "Value"],
            header_row_count=1,
        ),
    }
    structure = {"tables": [table]}
    out = build_chunks({}, {}, {}, structure)

    whole = next(c for c in out["chunks"] if c["chunk_id"] == "t0")
    assert whole["heading"] == "Balance Sheet Table"
    assert whole["section_type"] is None
    assert whole["page"] == 5
    assert whole["para_count"] is None
    assert whole["text"] == (
        "Balance Sheet Table\nValue\nCash: Value=100\nDebt: "
    )


def test_whole_table_chunk_capped_at_max_chars() -> None:
    long_value = "X" * 5000
    table = {
        "grid": _grid(
            rows=[["Item", "Value"], ["Cash", long_value]],
            column_headers=["Item", "Value"],
            header_row_count=1,
        ),
    }
    structure = {"tables": [table]}
    out = build_chunks({}, {}, {}, structure)

    whole = next(c for c in out["chunks"] if c["chunk_id"] == "t0")
    assert len(whole["text"]) == 4000


def test_table_context_falls_back_to_caption_then_table_index() -> None:
    table_no_caption = {
        "grid": _grid(
            rows=[["Item", "Value"], ["Cash", "1"]],
            column_headers=["Item", "Value"],
            header_row_count=1,
        ),
    }
    structure = {"tables": [{}, table_no_caption]}
    out = build_chunks({}, {}, {}, structure)

    row = next(c for c in out["chunks"] if c["chunk_id"] == "t1r1")
    assert row["heading"] == "Table 1"
    assert row["text"] == "Table 1 | Cash: Value=1"


def test_table_page_none_when_no_bounding_regions() -> None:
    table = {
        "grid": _grid(
            rows=[["Item", "Value"], ["Cash", "1"]],
            column_headers=["Item", "Value"],
            header_row_count=1,
        ),
    }
    structure = {"tables": [table]}
    out = build_chunks({}, {}, {}, structure)
    row = next(c for c in out["chunks"] if c["chunk_id"] == "t0r1")
    assert row["page"] is None


def test_dedup_removes_exact_duplicate_table_chunks() -> None:
    table = {
        "caption": {"content": "Same Caption"},
        "grid": _grid(
            rows=[["Item", "Value"], ["Cash", "1"]],
            column_headers=["Item", "Value"],
            header_row_count=1,
        ),
    }
    structure = {"tables": [table, dict(table)]}
    out = build_chunks({}, {}, {}, structure)
    ids = [c["chunk_id"] for c in out["chunks"]]
    # first table's row + whole-table chunks survive; second table's are byte-identical
    # duplicates and are dropped, WITHOUT renumbering the survivors
    assert ids == ["t0r1", "t0"]


def test_dedup_collapses_section_and_table_chunk_with_identical_text() -> None:
    # Crafted so the SECTION chunk's text ("H\nV\nCash: V=5") is byte-identical to the
    # table's WHOLE-TABLE chunk text (context "H" + header line "V" + row line
    # "Cash: V=5", newline-joined) -- proves the dedup pass runs over the combined
    # section+table list (decision 9), not just within one group. The section chunk,
    # emitted first, survives; the later-emitted whole-table chunk ("t0") is dropped.
    # The row chunk ("t0r1", text "H | Cash: V=5") is NOT a duplicate of anything and
    # survives untouched, with its id gap intact (no renumbering).
    ast = _ast([{"heading": "H", "paragraphs": ["V", "Cash: V=5"]}])
    table = {
        "caption": {"content": "H"},
        "grid": _grid(
            rows=[["Item", "V"], ["Cash", "5"]],
            column_headers=["Item", "V"],
            header_row_count=1,
        ),
    }
    structure = {"tables": [table]}
    out = build_chunks(ast, {}, {}, structure)
    ids = [c["chunk_id"] for c in out["chunks"]]
    assert ids == ["s0", "t0r1"]


def test_is_row_chunk_id_true_for_fine_grained_row_chunk() -> None:
    assert is_row_chunk_id("t3r5") is True
    assert is_row_chunk_id("t0r0") is True
    assert is_row_chunk_id("t12r34") is True


def test_is_row_chunk_id_false_for_section_and_whole_table_and_empty() -> None:
    assert is_row_chunk_id("t3") is False
    assert is_row_chunk_id("s0") is False
    assert is_row_chunk_id("") is False


def test_is_row_chunk_id_false_for_malformed_ids() -> None:
    assert is_row_chunk_id("t3r") is False
    assert is_row_chunk_id("tr5") is False
    assert is_row_chunk_id("t3r5x") is False
    assert is_row_chunk_id("xt3r5") is False
    assert is_row_chunk_id("s0r1") is False
