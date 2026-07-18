import pytest

from fdi.core.ocr_schema import normalize_ocr

_RAW = {
    "content": "ACME LTD\nDirectors' Report\nRevenue 100",
    "pages": [{"pageNumber": 1, "width": 8.5, "height": 11.0, "unit": "inch",
               "lines": [{"content": "ACME LTD"}],
               "words": [
                   {"content": "ACME", "confidence": 0.99,
                    "polygon": [0.0, 0.0, 0.5, 0.0, 0.5, 0.2, 0.0, 0.2]},
                   {"content": "LTD", "confidence": 0.4,
                    "polygon": [0.5, 0.0, 1.0, 0.0, 1.0, 0.2, 0.5, 0.2]},
               ],
               "spans": [{"offset": 0, "length": 8}],
               "selectionMarks": [
                   {"state": "selected", "confidence": 0.95,
                    "polygon": [3.0, 3.0, 3.1, 3.0, 3.1, 3.1, 3.0, 3.1]}
               ]}],
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
                           "kind": "columnHeader", "content": "Year ended 31 March",
                           "elements": ["/paragraphs/5"],
                           "boundingRegions": [
                               {"pageNumber": 1,
                                "polygon": [1.0, 1.0, 1.5, 1.0, 1.5, 1.2, 1.0, 1.2]}
                           ]}],
                "spans": [{"offset": 40, "length": 25}],
                "caption": {"content": "Table 1: Revenue",
                            "boundingRegions": [
                                {"pageNumber": 1,
                                 "polygon": [1.0, 0.9, 2.0, 0.9, 2.0, 1.0, 1.0, 1.0]}
                            ]},
                "footnotes": [
                    {"content": "Restated for FY23",
                     "boundingRegions": [
                         {"pageNumber": 1,
                          "polygon": [1.0, 1.6, 2.0, 1.6, 2.0, 1.7, 1.0, 1.7]}
                     ]}
                ]}],
    "figures": [
        {"caption": {"content": "Fig 1: Revenue trend"},
         "boundingRegions": [
             {"pageNumber": 1, "polygon": [2.0, 2.0, 3.0, 2.0, 3.0, 2.5, 2.0, 2.5]}
         ]},
        {"boundingRegions": [{"pageNumber": 1, "polygon": []}]},  # no "caption" key at all
    ],
    "sections": [{"elements": ["/paragraphs/0", "/sections/1"]}],
}


def test_normalize_preserves_structural_payload() -> None:
    out = normalize_ocr(_RAW)
    assert out["schema_version"] == 5
    assert out["content"].startswith("ACME LTD")
    assert out["paragraphs"][0] == {
        "role": "title", "content": "ACME LTD",
        "bounding_regions": [
            {"page_number": 1, "polygon": [0.0, 0.0, 1.0, 0.0, 1.0, 0.2, 0.0, 0.2]}
        ],
        "spans": [{"offset": 0, "length": 8}],
    }
    assert out["paragraphs"][1]["role"] is None and out["paragraphs"][1]["bounding_regions"] == []
    assert out["tables"][0]["cells"][0]["row_index"] == 0
    assert out["tables"][0]["cells"][0]["column_index"] == 0
    assert out["tables"][0]["cells"][0]["row_span"] == 1
    assert out["tables"][0]["cells"][0]["column_span"] == 2
    assert out["tables"][0]["cells"][0]["kind"] == "columnHeader"
    assert out["tables"][0]["cells"][0]["content"] == "Year ended 31 March"
    assert out["tables"][0]["bounding_regions"][0]["page_number"] == 1
    assert out["tables"][0]["spans"] == [{"offset": 40, "length": 25}]
    assert out["sections"] == [{"elements": ["/paragraphs/0", "/sections/1"]}]
    assert out["figures"][0] == {
        "caption": "Fig 1: Revenue trend",
        "bounding_regions": [
            {"page_number": 1, "polygon": [2.0, 2.0, 3.0, 2.0, 3.0, 2.5, 2.0, 2.5]}
        ],
    }
    assert out["figures"][1] == {
        "caption": "",
        "bounding_regions": [{"page_number": 1, "polygon": []}],
    }


def test_cell_carries_elements_and_bounding_regions() -> None:
    out = normalize_ocr(_RAW)
    cell = out["tables"][0]["cells"][0]
    assert cell["elements"] == ["/paragraphs/5"]
    assert cell["bounding_regions"] == [
        {"page_number": 1, "polygon": [1.0, 1.0, 1.5, 1.0, 1.5, 1.2, 1.0, 1.2]}
    ]


def test_table_carries_caption_and_footnotes() -> None:
    out = normalize_ocr(_RAW)
    table = out["tables"][0]
    assert table["caption"] == {
        "content": "Table 1: Revenue",
        "bounding_regions": [
            {"page_number": 1, "polygon": [1.0, 0.9, 2.0, 0.9, 2.0, 1.0, 1.0, 1.0]}
        ],
    }
    assert table["footnotes"] == [
        {
            "content": "Restated for FY23",
            "bounding_regions": [
                {"page_number": 1, "polygon": [1.0, 1.6, 2.0, 1.6, 2.0, 1.7, 1.0, 1.7]}
            ],
        }
    ]


def test_table_without_caption_or_footnotes_defaults() -> None:
    raw = {
        "content": "x",
        "tables": [{"rowCount": 1, "columnCount": 1, "cells": []}],
    }
    out = normalize_ocr(raw)
    assert out["tables"][0]["caption"] is None
    assert out["tables"][0]["footnotes"] == []


def test_cell_without_elements_or_bounding_regions_defaults() -> None:
    raw = {
        "content": "x",
        "tables": [{"rowCount": 1, "columnCount": 1,
                     "cells": [{"rowIndex": 0, "columnIndex": 0, "content": "1"}]}],
    }
    out = normalize_ocr(raw)
    cell = out["tables"][0]["cells"][0]
    assert cell["elements"] == []
    assert cell["bounding_regions"] == []


def test_page_carries_spans_and_selection_marks() -> None:
    out = normalize_ocr(_RAW)
    page = out["pages"][0]
    assert page["spans"] == [{"offset": 0, "length": 8}]
    assert page["selection_marks"] == [
        {
            "state": "selected",
            "confidence": 0.95,
            "polygon": [3.0, 3.0, 3.1, 3.0, 3.1, 3.1, 3.0, 3.1],
        }
    ]


def test_page_without_spans_or_selection_marks_defaults() -> None:
    raw = {
        "content": "x",
        "pages": [{"pageNumber": 1, "lines": [], "words": []}],
    }
    out = normalize_ocr(raw)
    page = out["pages"][0]
    assert page["spans"] == []
    assert page["selection_marks"] == []


def test_low_confidence_words_flags_only_below_threshold() -> None:
    out = normalize_ocr(_RAW)
    low = out["low_confidence_words"]
    assert len(low) == 1
    assert low[0]["page_number"] == 1
    assert low[0]["content"] == "LTD"
    assert low[0]["confidence"] == 0.4
    assert low[0]["polygon"] == [0.5, 0.0, 1.0, 0.0, 1.0, 0.2, 0.5, 0.2]


def test_low_confidence_words_skips_word_without_confidence() -> None:
    raw = {
        "content": "x",
        "pages": [{"pageNumber": 1, "lines": [], "words": [{"content": "foo"}]}],
    }
    out = normalize_ocr(raw)
    assert out["low_confidence_words"] == []


def test_empty_structure_is_empty_lists() -> None:
    out = normalize_ocr({"content": "x"})
    assert out["schema_version"] == 5
    assert out["paragraphs"] == []
    assert out["tables"] == []
    assert out["sections"] == []
    assert out["figures"] == []
    assert out["low_confidence_words"] == []


def test_missing_content_raises() -> None:
    with pytest.raises(ValueError, match="content"):
        normalize_ocr({"pages": []})
