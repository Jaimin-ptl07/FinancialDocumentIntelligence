import pytest

from fdi.core.ocr_schema import normalize_ocr

_RAW = {
    "content": "ACME LTD\nRevenue 100",
    "pages": [
        {
            "pageNumber": 1,
            "width": 8.5,
            "height": 11.0,
            "unit": "inch",
            "lines": [{"content": "ACME LTD"}, {"content": "Revenue 100"}],
            "words": [
                {"content": "ACME"},
                {"content": "LTD"},
                {"content": "Revenue"},
                {"content": "100"},
            ],
        }
    ],
    "paragraphs": [
        {"role": "title", "content": "ACME LTD"},
        {"content": "Revenue 100"},
    ],
    "tables": [{"rowCount": 2, "columnCount": 2, "cells": [{}, {}, {}, {}]}],
}


def test_normalize_extracts_stable_subset() -> None:
    out = normalize_ocr(_RAW)
    assert out["schema_version"] == 1
    assert out["content"] == "ACME LTD\nRevenue 100"
    assert out["pages"] == [{"page_number": 1, "width": 8.5, "height": 11.0, "unit": "inch",
                             "line_count": 2, "word_count": 4}]
    assert out["paragraphs"] == [{"role": "title", "content": "ACME LTD"},
                                 {"role": None, "content": "Revenue 100"}]
    assert out["tables"] == [{"row_count": 2, "column_count": 2, "cell_count": 4}]


def test_missing_content_raises() -> None:
    with pytest.raises(ValueError, match="content"):
        normalize_ocr({"pages": []})
