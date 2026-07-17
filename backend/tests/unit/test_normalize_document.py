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
    assert out["schema_version"] == 1
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
