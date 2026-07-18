from fdi.core.ast import build_ast


def test_build_ast_groups_by_role() -> None:
    unified = {"schema_version": 3, "content": "...", "page_count": 1, "blocks": [
        {"role": "title", "text": "ACME LTD Annual Report", "source_index": 0},
        {"role": "header", "text": "Confidential", "source_index": 1},
        {"role": "heading", "text": "Directors' Report", "source_index": 2},
        {"role": "body", "text": "The board is pleased.", "source_index": 3},
        {"role": "body", "text": "Revenue grew.", "source_index": 4},
        {"role": "heading", "text": "Auditor's Report", "source_index": 5},
        {"role": "body", "text": "We audited.", "source_index": 6},
        {"role": "page_number", "text": "12", "source_index": 7},
    ]}
    expected = {
        "schema_version": 2,
        "title": "ACME LTD Annual Report",
        "title_source_index": 0,
        "sections": [
            {
                "heading": "Directors' Report",
                "heading_source_index": 2,
                "paragraphs": ["The board is pleased.", "Revenue grew."],
                "paragraph_source_indices": [3, 4],
            },
            {
                "heading": "Auditor's Report",
                "heading_source_index": 5,
                "paragraphs": ["We audited."],
                "paragraph_source_indices": [6],
            },
        ],
    }
    assert build_ast(unified) == expected


def test_body_before_heading_is_a_preamble_section() -> None:
    unified = {"blocks": [
        {"role": "body", "text": "Preamble text.", "source_index": 0},
        {"role": "heading", "text": "Section One", "source_index": 1},
        {"role": "body", "text": "In section.", "source_index": 2},
    ]}
    assert build_ast(unified) == {
        "schema_version": 2, "title": None, "title_source_index": None,
        "sections": [
            {
                "heading": None,
                "heading_source_index": None,
                "paragraphs": ["Preamble text."],
                "paragraph_source_indices": [0],
            },
            {
                "heading": "Section One",
                "heading_source_index": 1,
                "paragraphs": ["In section."],
                "paragraph_source_indices": [2],
            },
        ],
    }


def test_empty_blocks_is_empty_ast() -> None:
    assert build_ast({}) == {
        "schema_version": 2, "title": None, "title_source_index": None, "sections": [],
    }


def test_second_title_becomes_a_section_and_table_cell_is_excluded() -> None:
    unified = {"blocks": [
        {"role": "title", "text": "ACME LTD Annual Report", "source_index": 0},
        {"role": "heading", "text": "Directors' Report", "source_index": 1},
        {"role": "body", "text": "The board is pleased.", "source_index": 2},
        {"role": "table_cell", "text": "2126.88", "source_index": 3},
        {"role": "title", "text": "Cash Flow Statement", "source_index": 4},
        {"role": "body", "text": "Net cash from operations.", "source_index": 5},
    ]}
    assert build_ast(unified) == {
        "schema_version": 2,
        "title": "ACME LTD Annual Report",
        "title_source_index": 0,
        "sections": [
            {
                "heading": "Directors' Report",
                "heading_source_index": 1,
                "paragraphs": ["The board is pleased."],
                "paragraph_source_indices": [2],
            },
            {
                "heading": "Cash Flow Statement",
                "heading_source_index": 4,
                "paragraphs": ["Net cash from operations."],
                "paragraph_source_indices": [5],
            },
        ],
    }


def test_source_index_missing_or_non_int_degrades_to_none() -> None:
    unified = {"blocks": [
        {"role": "title", "text": "T"},  # no source_index at all
        {"role": "heading", "text": "H", "source_index": "not-an-int"},
        {"role": "body", "text": "B", "source_index": None},
    ]}
    ast = build_ast(unified)
    assert ast["title_source_index"] is None
    assert ast["sections"][0]["heading_source_index"] is None
    assert ast["sections"][0]["paragraph_source_indices"] == [None]
