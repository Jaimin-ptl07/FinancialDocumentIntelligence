from fdi.core.links import extract_links


def test_extracts_figures_and_footnotes() -> None:
    ocr = {
        "figures": [
            {"caption": "Fig 1: Revenue trend", "bounding_regions": []},
            {"caption": "Fig 2: Segment mix", "bounding_regions": []},
        ],
        "paragraphs": [
            {"role": "title", "content": "ACME LTD"},
            {"role": "footnote", "content": "1. Restated for FY23 comparatives."},
            {"role": None, "content": "Revenue 100"},
        ],
    }
    out = extract_links(ocr)
    assert out == {
        "schema_version": 1,
        "figures": [{"caption": "Fig 1: Revenue trend"}, {"caption": "Fig 2: Segment mix"}],
        "footnotes": [{"text": "1. Restated for FY23 comparatives."}],
    }


def test_no_figures_or_footnotes_is_empty_lists() -> None:
    ocr = {"figures": [], "paragraphs": [{"role": "title", "content": "ACME LTD"}]}
    assert extract_links(ocr) == {"schema_version": 1, "figures": [], "footnotes": []}


def test_missing_keys_default_to_empty() -> None:
    assert extract_links({}) == {"schema_version": 1, "figures": [], "footnotes": []}
