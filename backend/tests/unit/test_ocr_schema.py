import pytest

from fdi.core.ocr_schema import normalize_ocr

_RAW = {
    "content": "ACME LTD\nDirectors' Report\nRevenue 100",
    "pages": [{"pageNumber": 1, "width": 8.5, "height": 11.0, "unit": "inch",
               "lines": [{"content": "ACME LTD"}],
               "words": [{"content": "ACME"}, {"content": "LTD"}]}],
    "paragraphs": [
        {"role": "title", "content": "ACME LTD",
         "boundingRegions": [
             {"pageNumber": 1, "polygon": [0.0, 0.0, 1.0, 0.0, 1.0, 0.2, 0.0, 0.2]}
         ],
         "spans": [{"offset": 0, "length": 8}]},
        {"content": "Revenue 100", "boundingRegions": [],
         "spans": [{"offset": 9, "length": 11}]},
    ],
    "tables": [{"rowCount": 1, "columnCount": 2,
                "boundingRegions": [
                    {"pageNumber": 1, "polygon": [1.0, 1.0, 2.0, 1.0, 2.0, 1.5, 1.0, 1.5]}
                ],
                "cells": [{"rowIndex": 0, "columnIndex": 0, "rowSpan": 1, "columnSpan": 2,
                           "kind": "columnHeader", "content": "Year ended 31 March"}]}],
    "sections": [{"elements": ["/paragraphs/0", "/sections/1"]}],
}


def test_normalize_preserves_structural_payload() -> None:
    out = normalize_ocr(_RAW)
    assert out["schema_version"] == 2
    assert out["content"].startswith("ACME LTD")
    assert out["paragraphs"][0] == {
        "role": "title", "content": "ACME LTD",
        "bounding_regions": [
            {"page_number": 1, "polygon": [0.0, 0.0, 1.0, 0.0, 1.0, 0.2, 0.0, 0.2]}
        ],
        "spans": [{"offset": 0, "length": 8}],
    }
    assert out["paragraphs"][1]["role"] is None and out["paragraphs"][1]["bounding_regions"] == []
    assert out["tables"][0]["cells"][0] == {
        "row_index": 0, "column_index": 0, "row_span": 1, "column_span": 2,
        "kind": "columnHeader", "content": "Year ended 31 March",
    }
    assert out["tables"][0]["bounding_regions"][0]["page_number"] == 1
    assert out["sections"] == [{"elements": ["/paragraphs/0", "/sections/1"]}]


def test_empty_structure_is_empty_lists() -> None:
    out = normalize_ocr({"content": "x"})
    assert out["schema_version"] == 2
    assert out["paragraphs"] == []
    assert out["tables"] == []
    assert out["sections"] == []


def test_missing_content_raises() -> None:
    with pytest.raises(ValueError, match="content"):
        normalize_ocr({"pages": []})
