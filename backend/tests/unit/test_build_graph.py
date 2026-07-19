from fdi.core.graph import build_graph


def test_one_section_two_paragraphs_builds_contains_and_reading_order() -> None:
    ast = {
        "schema_version": 2,
        "title": "ACME LTD Annual Report",
        "title_source_index": 0,
        "sections": [
            {
                "heading": "Directors' Report",
                "heading_source_index": 1,
                "paragraphs": ["The board is pleased.", "Revenue grew."],
                "paragraph_source_indices": [2, 3],
            },
        ],
    }
    assert build_graph(ast, {}) == {
        "schema_version": 3,
        "nodes": [
            {"id": "doc", "type": "document", "label": "ACME LTD Annual Report",
             "source_index": 0},
            {"id": "s0", "type": "section", "label": "Directors' Report", "source_index": 1},
            {"id": "s0p0", "type": "paragraph", "label": "The board is pleased.",
             "source_index": 2},
            {"id": "s0p1", "type": "paragraph", "label": "Revenue grew.", "source_index": 3},
        ],
        "edges": [
            {"source": "doc", "target": "s0", "type": "CONTAINS"},
            {"source": "s0", "target": "s0p0", "type": "CONTAINS"},
            {"source": "s0", "target": "s0p1", "type": "CONTAINS"},
            {"source": "s0p0", "target": "s0p1", "type": "READING_ORDER"},
        ],
    }


def test_empty_ast_is_just_the_document_node() -> None:
    assert build_graph({}, {}) == {
        "schema_version": 3,
        "nodes": [{"id": "doc", "type": "document", "label": "", "source_index": None}],
        "edges": [],
    }


def test_multi_section_adds_inter_section_reading_order() -> None:
    ast = {
        "title": None,
        "sections": [
            {"heading": "Section One", "heading_source_index": 0, "paragraphs": ["Para A."],
             "paragraph_source_indices": [1]},
            {"heading": "Section Two", "heading_source_index": 2, "paragraphs": ["Para B."],
             "paragraph_source_indices": [3]},
        ],
    }
    assert build_graph(ast, {}) == {
        "schema_version": 3,
        "nodes": [
            {"id": "doc", "type": "document", "label": "", "source_index": None},
            {"id": "s0", "type": "section", "label": "Section One", "source_index": 0},
            {"id": "s0p0", "type": "paragraph", "label": "Para A.", "source_index": 1},
            {"id": "s1", "type": "section", "label": "Section Two", "source_index": 2},
            {"id": "s1p0", "type": "paragraph", "label": "Para B.", "source_index": 3},
        ],
        "edges": [
            {"source": "doc", "target": "s0", "type": "CONTAINS"},
            {"source": "s0", "target": "s0p0", "type": "CONTAINS"},
            {"source": "doc", "target": "s1", "type": "CONTAINS"},
            {"source": "s0", "target": "s1", "type": "READING_ORDER"},
            {"source": "s1", "target": "s1p0", "type": "CONTAINS"},
        ],
    }


def test_paragraph_label_is_not_truncated() -> None:
    long_para = "x" * 200
    ast = {"sections": [{"heading": "H", "paragraphs": [long_para]}]}
    result = build_graph(ast, {})
    para_node = next(n for n in result["nodes"] if n["id"] == "s0p0")
    assert para_node["label"] == "x" * 200


def test_missing_title_and_sections_degrade_to_empty() -> None:
    assert build_graph({"title": None, "sections": None}, {}) == {
        "schema_version": 3,
        "nodes": [{"id": "doc", "type": "document", "label": "", "source_index": None}],
        "edges": [],
    }


def test_non_str_heading_degrades_to_empty_label() -> None:
    ast = {"sections": [{"heading": 123, "paragraphs": []}]}
    result = build_graph(ast, {})
    section_node = next(n for n in result["nodes"] if n["id"] == "s0")
    assert section_node["label"] == ""


def test_non_int_source_index_degrades_to_none_on_every_node_type() -> None:
    ast = {
        "title": "T",
        "title_source_index": "not-an-int",
        "sections": [
            {
                "heading": "H",
                "heading_source_index": None,
                "paragraphs": ["P"],
                "paragraph_source_indices": ["also-not-an-int"],
            },
        ],
    }
    result = build_graph(ast, {})
    by_id = {n["id"]: n for n in result["nodes"]}
    assert by_id["doc"]["source_index"] is None
    assert by_id["s0"]["source_index"] is None
    assert by_id["s0p0"]["source_index"] is None


def test_missing_paragraph_source_indices_list_degrades_to_none() -> None:
    ast = {"sections": [{"heading": "H", "paragraphs": ["P0", "P1"]}]}
    result = build_graph(ast, {})
    by_id = {n["id"]: n for n in result["nodes"]}
    assert by_id["s0p0"]["source_index"] is None
    assert by_id["s0p1"]["source_index"] is None


def _ast_one_section(
    table_refs: list[int], figure_refs: list[dict[str, object]]
) -> dict[str, object]:
    return {
        "sections": [
            {
                "heading": "Balance Sheet",
                "heading_source_index": 0,
                "paragraphs": [],
                "paragraph_source_indices": [],
                "table_refs": table_refs,
                "figure_refs": figure_refs,
            },
        ],
    }


def test_table_ref_with_no_source_indices_key_uses_identity_bridge() -> None:
    ast = _ast_one_section([0], [])
    structure = {"tables": [{"caption": {"content": "Total Assets"}}]}
    result = build_graph(ast, structure)
    table_node = next(n for n in result["nodes"] if n["id"] == "t0")
    assert table_node == {
        "id": "t0", "type": "table", "label": "Total Assets", "source_index": None,
    }
    assert {"source": "s0", "target": "t0", "type": "CONTAINS"} in result["edges"]


def test_table_ref_bridged_via_explicit_source_indices() -> None:
    ast = _ast_one_section([1], [])
    structure = {
        "tables": [
            {"caption": None},
            {"caption": None},
            {"caption": {"content": "Cash Flow"}, "source_indices": [0, 1]},
        ],
    }
    result = build_graph(ast, structure)
    table_node = next(n for n in result["nodes"] if n["id"] == "t2")
    assert table_node["label"] == "Cash Flow"
    assert {"source": "s0", "target": "t2", "type": "CONTAINS"} in result["edges"]
    assert not any(n["id"] in {"t0", "t1"} for n in result["nodes"])


def test_table_with_no_caption_falls_back_to_table_i_label() -> None:
    ast = _ast_one_section([0], [])
    structure = {"tables": [{"caption": None}]}
    result = build_graph(ast, structure)
    table_node = next(n for n in result["nodes"] if n["id"] == "t0")
    assert table_node["label"] == "Table 0"


def test_unmapped_raw_table_index_is_skipped_no_raise() -> None:
    ast = _ast_one_section([5], [])
    structure = {"tables": [{"caption": None}]}
    result = build_graph(ast, structure)
    assert not any(n["type"] == "table" for n in result["nodes"])
    assert not any(e["type"] == "CONTAINS" and e["target"] == "t5" for e in result["edges"])


def test_figure_ref_produces_figure_node_and_contains_edge() -> None:
    ast = _ast_one_section([], [{"index": 3, "caption": "Fig 3: Org chart"}])
    result = build_graph(ast, {})
    fig_node = next(n for n in result["nodes"] if n["id"] == "f3")
    assert fig_node == {
        "id": "f3", "type": "figure", "label": "Fig 3: Org chart", "source_index": None,
    }
    assert {"source": "s0", "target": "f3", "type": "CONTAINS"} in result["edges"]


def test_figure_ref_blank_caption_falls_back_to_figure_k_label() -> None:
    ast = _ast_one_section([], [{"index": 2, "caption": ""}])
    result = build_graph(ast, {})
    fig_node = next(n for n in result["nodes"] if n["id"] == "f2")
    assert fig_node["label"] == "Figure 2"


def test_same_raw_table_index_in_two_sections_first_section_wins() -> None:
    ast = {
        "sections": [
            {
                "heading": "A", "heading_source_index": 0, "paragraphs": [],
                "paragraph_source_indices": [], "table_refs": [0], "figure_refs": [],
            },
            {
                "heading": "B", "heading_source_index": 1, "paragraphs": [],
                "paragraph_source_indices": [], "table_refs": [0], "figure_refs": [],
            },
        ],
    }
    structure = {"tables": [{"caption": {"content": "Shared"}}]}
    result = build_graph(ast, structure)
    table_nodes = [n for n in result["nodes"] if n["type"] == "table"]
    assert len(table_nodes) == 1
    contains_to_t0 = [e for e in result["edges"] if e["target"] == "t0"]
    assert contains_to_t0 == [{"source": "s0", "target": "t0", "type": "CONTAINS"}]


def test_no_reading_order_edge_ever_touches_a_table_or_figure_node() -> None:
    ast = _ast_one_section([0], [{"index": 0, "caption": "Fig"}])
    structure = {"tables": [{"caption": None}]}
    result = build_graph(ast, structure)
    for e in result["edges"]:
        if e["type"] == "READING_ORDER":
            assert not str(e["source"]).startswith(("t", "f"))
            assert not str(e["target"]).startswith(("t", "f"))


def test_schema_version_is_3() -> None:
    assert build_graph({}, {})["schema_version"] == 3


def test_missing_table_refs_key_degrades_to_no_table_nodes() -> None:
    ast = {"sections": [{"heading": "H", "paragraphs": []}]}
    result = build_graph(ast, {"tables": [{"caption": None}]})
    assert not any(n["type"] == "table" for n in result["nodes"])
