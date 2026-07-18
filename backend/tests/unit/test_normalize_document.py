import pytest

from fdi.core.unified import normalize_document

_OCR = {
    "schema_version": 1,
    "content": "ACME LTD\nDirectors' Report\nThe board ...",
    "pages": [{"page_number": 1}, {"page_number": 2}],
    "paragraphs": [
        {"role": "title", "content": "ACME LTD"},
        {"role": "sectionHeading", "content": "Directors' Report"},
        {"content": "The board ..."},
    ],
    "tables": [],
}


def test_normalize_document_builds_unified_doc() -> None:
    out = normalize_document(_OCR)
    assert out["schema_version"] == 2
    assert out["content"].startswith("ACME LTD")
    assert out["page_count"] == 2
    assert out["blocks"] == [
        {"role": "title", "text": "ACME LTD"},
        {"role": "heading", "text": "Directors' Report"},
        {"role": "body", "text": "The board ..."},
    ]


def test_missing_content_raises() -> None:
    with pytest.raises(ValueError, match="content"):
        normalize_document({"paragraphs": []})


def test_table_cell_paragraph_is_reroled_not_dropped() -> None:
    ocr = {
        "content": "ACME LTD",
        "pages": [],
        "paragraphs": [
            {"role": "title", "content": "ACME LTD"},
            {"content": "2126.88"},
            {"role": "sectionHeading", "content": "Balance Sheet"},
        ],
        "tables": [
            {"cells": [{"elements": ["/paragraphs/1"]}]},
        ],
    }
    out = normalize_document(ocr)
    blocks = out["blocks"]
    assert len(blocks) == len(ocr["paragraphs"])
    assert blocks[0] == {"role": "title", "text": "ACME LTD"}
    assert blocks[1] == {"role": "table_cell", "text": "2126.88"}
    assert blocks[2] == {"role": "heading", "text": "Balance Sheet"}
    assert out["schema_version"] == 2


def test_malformed_table_cell_elements_do_not_reroled_or_raise() -> None:
    ocr = {
        "content": "ACME LTD",
        "pages": [],
        "paragraphs": [
            {"content": "hello"},
        ],
        "tables": [
            {"cells": [{"elements": ["not-a-paragraph-ref", "/paragraphs/notanint"]}]},
            {"cells": [{"elements": None}]},
            {"cells": None},
            "not-a-mapping",
        ],
    }
    out = normalize_document(ocr)
    assert out["blocks"] == [{"role": "body", "text": "hello"}]


def test_no_tables_behaves_as_before() -> None:
    ocr = {
        "content": "ACME LTD",
        "pages": [],
        "paragraphs": [{"content": "hello"}],
    }
    out = normalize_document(ocr)
    assert out["blocks"] == [{"role": "body", "text": "hello"}]
    assert out["schema_version"] == 2
