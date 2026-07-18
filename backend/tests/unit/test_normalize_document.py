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
    assert out["schema_version"] == 3
    assert out["content"].startswith("ACME LTD")
    assert out["page_count"] == 2
    assert out["blocks"] == [
        {"role": "title", "text": "ACME LTD", "source_index": 0},
        {"role": "heading", "text": "Directors' Report", "source_index": 1},
        {"role": "body", "text": "The board ...", "source_index": 2},
    ]


def test_every_block_source_index_equals_its_own_ocr_paragraph_position() -> None:
    ocr = {
        "content": "A\nB\nC",
        "pages": [],
        "paragraphs": [
            {"content": "A"},
            {"content": "B"},
            {"content": "C"},
        ],
        "tables": [],
    }
    out = normalize_document(ocr)
    assert [b["source_index"] for b in out["blocks"]] == [0, 1, 2]


def test_malformed_paragraph_entry_leaves_no_source_index_gap() -> None:
    ocr = {
        "content": "A\nC",
        "pages": [],
        "paragraphs": [
            {"content": "A"},
            "not-a-dict",
            {"content": "C"},
        ],
        "tables": [],
    }
    out = normalize_document(ocr)
    # index 1 was malformed and produced no block at all -- the surviving
    # blocks still carry their TRUE ocr-paragraph position, not a
    # re-numbered 0/1 over the shrunk blocks list.
    assert [b["source_index"] for b in out["blocks"]] == [0, 2]


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
    assert blocks[0] == {"role": "title", "text": "ACME LTD", "source_index": 0}
    assert blocks[1] == {"role": "table_cell", "text": "2126.88", "source_index": 1}
    assert blocks[2] == {"role": "heading", "text": "Balance Sheet", "source_index": 2}
    assert out["schema_version"] == 3


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
    assert out["blocks"] == [{"role": "body", "text": "hello", "source_index": 0}]


def test_no_tables_behaves_as_before() -> None:
    ocr = {
        "content": "ACME LTD",
        "pages": [],
        "paragraphs": [{"content": "hello"}],
    }
    out = normalize_document(ocr)
    assert out["blocks"] == [{"role": "body", "text": "hello", "source_index": 0}]
    assert out["schema_version"] == 3
