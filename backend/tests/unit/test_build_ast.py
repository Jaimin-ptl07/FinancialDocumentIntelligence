from fdi.core.ast import build_ast


def test_build_ast_groups_by_role() -> None:
    unified = {"schema_version": 1, "content": "...", "page_count": 1, "blocks": [
        {"role": "title", "text": "ACME LTD Annual Report"},
        {"role": "header", "text": "Confidential"},
        {"role": "heading", "text": "Directors' Report"},
        {"role": "body", "text": "The board is pleased."},
        {"role": "body", "text": "Revenue grew."},
        {"role": "heading", "text": "Auditor's Report"},
        {"role": "body", "text": "We audited."},
        {"role": "page_number", "text": "12"},
    ]}
    expected = {
        "schema_version": 1,
        "title": "ACME LTD Annual Report",
        "sections": [
            {
                "heading": "Directors' Report",
                "paragraphs": ["The board is pleased.", "Revenue grew."],
            },
            {"heading": "Auditor's Report", "paragraphs": ["We audited."]},
        ],
    }
    assert build_ast(unified) == expected


def test_body_before_heading_is_a_preamble_section() -> None:
    unified = {"blocks": [
        {"role": "body", "text": "Preamble text."},
        {"role": "heading", "text": "Section One"},
        {"role": "body", "text": "In section."},
    ]}
    assert build_ast(unified) == {
        "schema_version": 1, "title": None,
        "sections": [
            {"heading": None, "paragraphs": ["Preamble text."]},
            {"heading": "Section One", "paragraphs": ["In section."]},
        ],
    }


def test_empty_blocks_is_empty_ast() -> None:
    assert build_ast({}) == {"schema_version": 1, "title": None, "sections": []}


def test_second_title_becomes_a_section_and_table_cell_is_excluded() -> None:
    unified = {"blocks": [
        {"role": "title", "text": "ACME LTD Annual Report"},
        {"role": "heading", "text": "Directors' Report"},
        {"role": "body", "text": "The board is pleased."},
        {"role": "table_cell", "text": "2126.88"},
        {"role": "title", "text": "Cash Flow Statement"},
        {"role": "body", "text": "Net cash from operations."},
    ]}
    assert build_ast(unified) == {
        "schema_version": 1,
        "title": "ACME LTD Annual Report",
        "sections": [
            {
                "heading": "Directors' Report",
                "paragraphs": ["The board is pleased."],
            },
            {
                "heading": "Cash Flow Statement",
                "paragraphs": ["Net cash from operations."],
            },
        ],
    }
