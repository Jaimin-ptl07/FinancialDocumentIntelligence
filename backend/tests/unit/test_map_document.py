from fdi.core.ontology import map_document

_AST = {
    "schema_version": 1,
    "title": "ACME LTD Annual Report",
    "sections": [
        {"heading": "Revenue", "paragraphs": ["Sales", "Some narrative text with no concept."]},
        {"heading": "Balance Sheet", "paragraphs": ["Total Assets", "Total Liabilities"]},
    ],
}


def test_map_document_maps_headings_and_paragraphs() -> None:
    out = map_document(_AST)
    assert out["schema_version"] == 1
    assert out["mappings"] == [
        {"label": "Revenue", "concept": "REVENUE"},
        {"label": "Sales", "concept": "REVENUE"},
        {"label": "Total Assets", "concept": "TOTAL_ASSETS"},
        {"label": "Total Liabilities", "concept": "TOTAL_LIABILITIES"},
    ]


def test_map_document_no_sections_is_empty_mappings() -> None:
    assert map_document({}) == {"schema_version": 1, "mappings": []}
    assert map_document({"sections": []}) == {"schema_version": 1, "mappings": []}


def test_map_document_malformed_section_is_skipped() -> None:
    ast = {
        "sections": [
            {"heading": "Revenue"},
            "not a dict",
            {"heading": None, "paragraphs": [1, "Sales"]},
        ]
    }
    out = map_document(ast)
    assert out["mappings"] == [
        {"label": "Revenue", "concept": "REVENUE"},
        {"label": "Sales", "concept": "REVENUE"},
    ]
