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
    assert build_graph(ast) == {
        "schema_version": 2,
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
    assert build_graph({}) == {
        "schema_version": 2,
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
    assert build_graph(ast) == {
        "schema_version": 2,
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
    result = build_graph(ast)
    para_node = next(n for n in result["nodes"] if n["id"] == "s0p0")
    assert para_node["label"] == "x" * 200


def test_missing_title_and_sections_degrade_to_empty() -> None:
    assert build_graph({"title": None, "sections": None}) == {
        "schema_version": 2,
        "nodes": [{"id": "doc", "type": "document", "label": "", "source_index": None}],
        "edges": [],
    }


def test_non_str_heading_degrades_to_empty_label() -> None:
    ast = {"sections": [{"heading": 123, "paragraphs": []}]}
    result = build_graph(ast)
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
    result = build_graph(ast)
    by_id = {n["id"]: n for n in result["nodes"]}
    assert by_id["doc"]["source_index"] is None
    assert by_id["s0"]["source_index"] is None
    assert by_id["s0p0"]["source_index"] is None


def test_missing_paragraph_source_indices_list_degrades_to_none() -> None:
    ast = {"sections": [{"heading": "H", "paragraphs": ["P0", "P1"]}]}
    result = build_graph(ast)
    by_id = {n["id"]: n for n in result["nodes"]}
    assert by_id["s0p0"]["source_index"] is None
    assert by_id["s0p1"]["source_index"] is None
