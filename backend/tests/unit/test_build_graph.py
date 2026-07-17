from fdi.core.graph import build_graph


def test_one_section_two_paragraphs_builds_contains_and_reading_order() -> None:
    ast = {
        "schema_version": 1,
        "title": "ACME LTD Annual Report",
        "sections": [
            {
                "heading": "Directors' Report",
                "paragraphs": ["The board is pleased.", "Revenue grew."],
            },
        ],
    }
    assert build_graph(ast) == {
        "schema_version": 1,
        "nodes": [
            {"id": "doc", "type": "document", "label": "ACME LTD Annual Report"},
            {"id": "s0", "type": "section", "label": "Directors' Report"},
            {"id": "s0p0", "type": "paragraph", "label": "The board is pleased."},
            {"id": "s0p1", "type": "paragraph", "label": "Revenue grew."},
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
        "schema_version": 1,
        "nodes": [{"id": "doc", "type": "document", "label": ""}],
        "edges": [],
    }


def test_multi_section_adds_inter_section_reading_order() -> None:
    ast = {
        "title": None,
        "sections": [
            {"heading": "Section One", "paragraphs": ["Para A."]},
            {"heading": "Section Two", "paragraphs": ["Para B."]},
        ],
    }
    assert build_graph(ast) == {
        "schema_version": 1,
        "nodes": [
            {"id": "doc", "type": "document", "label": ""},
            {"id": "s0", "type": "section", "label": "Section One"},
            {"id": "s0p0", "type": "paragraph", "label": "Para A."},
            {"id": "s1", "type": "section", "label": "Section Two"},
            {"id": "s1p0", "type": "paragraph", "label": "Para B."},
        ],
        "edges": [
            {"source": "doc", "target": "s0", "type": "CONTAINS"},
            {"source": "s0", "target": "s0p0", "type": "CONTAINS"},
            {"source": "doc", "target": "s1", "type": "CONTAINS"},
            {"source": "s0", "target": "s1", "type": "READING_ORDER"},
            {"source": "s1", "target": "s1p0", "type": "CONTAINS"},
        ],
    }


def test_paragraph_label_truncated_to_80_chars() -> None:
    long_para = "x" * 200
    ast = {"sections": [{"heading": "H", "paragraphs": [long_para]}]}
    result = build_graph(ast)
    para_node = next(n for n in result["nodes"] if n["id"] == "s0p0")
    assert para_node["label"] == "x" * 80


def test_missing_title_and_sections_degrade_to_empty() -> None:
    assert build_graph({"title": None, "sections": None}) == {
        "schema_version": 1,
        "nodes": [{"id": "doc", "type": "document", "label": ""}],
        "edges": [],
    }


def test_non_str_heading_degrades_to_empty_label() -> None:
    ast = {"sections": [{"heading": 123, "paragraphs": []}]}
    result = build_graph(ast)
    section_node = next(n for n in result["nodes"] if n["id"] == "s0")
    assert section_node["label"] == ""
