from fdi.core.provenance import build_provenance

_REGIONS_TITLE = [{"page_number": 1, "polygon": [0, 0, 1, 0, 1, 1, 0, 1]}]
_REGIONS_HEADING = [{"page_number": 1, "polygon": [0, 1, 1, 1, 1, 2, 0, 2]}]
_REGIONS_PARA = [{"page_number": 2, "polygon": [0, 0, 1, 0, 1, 1, 0, 1]}]

_OCR = {
    "schema_version": 5,
    "content": "ACME",
    "paragraphs": [
        {
            "role": "title",
            "content": "ACME LTD Annual Report",
            "bounding_regions": _REGIONS_TITLE,
            "spans": [],
        },
        {
            "role": "sectionHeading",
            "content": "Directors' Report",
            "bounding_regions": _REGIONS_HEADING,
            "spans": [],
        },
        {
            "content": (
                "The board is pleased with results. Revenue grew twelve percent year over year."
            ),
            "bounding_regions": _REGIONS_PARA,
            "spans": [],
        },
    ],
}

_GRAPH = {
    "schema_version": 2,
    "nodes": [
        {"id": "doc", "type": "document", "label": "ACME LTD Annual Report", "source_index": 0},
        {"id": "s0", "type": "section", "label": "Directors' Report", "source_index": 1},
        {
            "id": "s0p0", "type": "paragraph",
            "label": (
                "The board is pleased with results. "
                "Revenue grew twelve percent year over year."
            ),
            "source_index": 2,
        },
    ],
    "edges": [],
}


def test_paragraph_node_direct_index_lookup_returns_page_and_regions() -> None:
    out = build_provenance(_GRAPH, _OCR)
    entry = next(e for e in out["node_provenance"] if e["node_id"] == "s0p0")
    assert entry == {"node_id": "s0p0", "page": 2, "bounding_regions": _REGIONS_PARA}


def test_section_node_direct_index_lookup_returns_page_and_regions() -> None:
    out = build_provenance(_GRAPH, _OCR)
    entry = next(e for e in out["node_provenance"] if e["node_id"] == "s0")
    assert entry == {"node_id": "s0", "page": 1, "bounding_regions": _REGIONS_HEADING}


def test_document_node_with_valid_source_index_is_a_direct_hit() -> None:
    out = build_provenance(_GRAPH, _OCR)
    entry = next(e for e in out["node_provenance"] if e["node_id"] == "doc")
    assert entry == {"node_id": "doc", "page": 1, "bounding_regions": _REGIONS_TITLE}


def test_document_node_with_no_source_index_falls_back_to_first_paragraph_with_regions() -> None:
    graph = {"nodes": [{"id": "doc", "type": "document", "label": "x"}]}
    out = build_provenance(graph, _OCR)
    entry = next(e for e in out["node_provenance"] if e["node_id"] == "doc")
    assert entry == {"node_id": "doc", "page": 1, "bounding_regions": _REGIONS_TITLE}


def test_two_identically_labeled_section_nodes_resolve_to_different_correct_pages() -> None:
    """The core regression case (spec §5): two 'Particulars' section nodes at
    different source_index values must resolve to their OWN pages, not both
    collapse to wherever 'Particulars' first appears -- there is no longer
    any text matching to be ambiguous about."""
    ocr = {
        "paragraphs": [
            {
                "content": "Particulars",
                "bounding_regions": [{"page_number": 2, "polygon": []}],
                "spans": [],
            },
            {
                "content": "Particulars",
                "bounding_regions": [{"page_number": 83, "polygon": []}],
                "spans": [],
            },
        ],
    }
    graph = {
        "nodes": [
            {"id": "s0", "type": "section", "label": "Particulars", "source_index": 0},
            {"id": "s1", "type": "section", "label": "Particulars", "source_index": 1},
        ],
    }
    out = build_provenance(graph, ocr)
    by_id = {e["node_id"]: e for e in out["node_provenance"]}
    assert by_id["s0"]["page"] == 2
    assert by_id["s1"]["page"] == 83


def test_missing_source_index_is_omitted() -> None:
    graph = {"nodes": [{"id": "s0", "type": "section", "label": "X"}]}
    out = build_provenance(graph, _OCR)
    assert out["node_provenance"] == []


def test_none_source_index_is_omitted() -> None:
    graph = {"nodes": [{"id": "s0", "type": "section", "label": "X", "source_index": None}]}
    out = build_provenance(graph, _OCR)
    assert out["node_provenance"] == []


def test_non_int_source_index_is_omitted() -> None:
    graph = {
        "nodes": [{"id": "s0", "type": "section", "label": "X", "source_index": "not-an-int"}]
    }
    out = build_provenance(graph, _OCR)
    assert out["node_provenance"] == []


def test_bool_source_index_is_omitted() -> None:
    graph = {"nodes": [{"id": "s0", "type": "section", "label": "X", "source_index": True}]}
    out = build_provenance(graph, _OCR)
    assert out["node_provenance"] == []


def test_negative_source_index_is_omitted() -> None:
    graph = {"nodes": [{"id": "s0", "type": "section", "label": "X", "source_index": -1}]}
    out = build_provenance(graph, _OCR)
    assert out["node_provenance"] == []


def test_out_of_range_source_index_is_omitted() -> None:
    graph = {"nodes": [{"id": "s0", "type": "section", "label": "X", "source_index": 999}]}
    out = build_provenance(graph, _OCR)
    assert out["node_provenance"] == []


def test_source_index_pointing_at_non_mapping_paragraph_is_omitted() -> None:
    ocr = {"paragraphs": ["not-a-mapping"]}
    graph = {"nodes": [{"id": "s0", "type": "section", "label": "X", "source_index": 0}]}
    out = build_provenance(graph, ocr)
    assert out["node_provenance"] == []


def test_source_index_pointing_at_empty_bounding_regions_is_omitted() -> None:
    ocr = {
        "paragraphs": [
            {"content": "X", "bounding_regions": [], "spans": []},
        ],
    }
    graph = {"nodes": [{"id": "s0", "type": "section", "label": "X", "source_index": 0}]}
    out = build_provenance(graph, ocr)
    assert out["node_provenance"] == []


def test_empty_graph_yields_empty_node_provenance() -> None:
    assert build_provenance({}, _OCR) == {"schema_version": 1, "node_provenance": []}
    assert build_provenance({"nodes": []}, _OCR) == {"schema_version": 1, "node_provenance": []}


def test_malformed_ocr_paragraphs_degrade_without_raising() -> None:
    ocr = {
        "paragraphs": [
            "not a mapping",
            {"content": None, "bounding_regions": [], "spans": []},
            {"content": "Directors' Report", "bounding_regions": "not a list", "spans": []},
            {
                "content": "Directors' Report",
                "bounding_regions": [{"page_number": "not an int", "polygon": []}],
                "spans": [],
            },
        ],
    }
    graph = {
        "nodes": [
            {"id": "s0", "type": "section", "label": "Directors' Report", "source_index": 0},
            {"id": "s1", "type": "section", "label": "Directors' Report", "source_index": 1},
            {"id": "s2", "type": "section", "label": "Directors' Report", "source_index": 2},
            {"id": "s3", "type": "section", "label": "Directors' Report", "source_index": 3},
        ],
    }
    out = build_provenance(graph, ocr)
    assert out == {"schema_version": 1, "node_provenance": []}
