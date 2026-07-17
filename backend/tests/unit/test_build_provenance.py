from fdi.core.provenance import build_provenance

_GRAPH = {
    "schema_version": 1,
    "nodes": [
        {"id": "doc", "type": "document", "label": "ACME LTD Annual Report"},
        {"id": "s0", "type": "section", "label": "Directors' Report"},
        {"id": "s0p0", "type": "paragraph", "label": "The board is pleased with results."},
    ],
    "edges": [],
}

_REGIONS_TITLE = [{"page_number": 1, "polygon": [0, 0, 1, 0, 1, 1, 0, 1]}]
_REGIONS_HEADING = [{"page_number": 1, "polygon": [0, 1, 1, 1, 1, 2, 0, 2]}]
_REGIONS_PARA = [{"page_number": 2, "polygon": [0, 0, 1, 0, 1, 1, 0, 1]}]

_OCR = {
    "schema_version": 4,
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


def test_paragraph_node_prefix_match_returns_page_and_regions() -> None:
    out = build_provenance(_GRAPH, _OCR)
    entry = next(e for e in out["node_provenance"] if e["node_id"] == "s0p0")
    assert entry == {"node_id": "s0p0", "page": 2, "bounding_regions": _REGIONS_PARA}


def test_section_node_exact_heading_match_returns_page_and_regions() -> None:
    out = build_provenance(_GRAPH, _OCR)
    entry = next(e for e in out["node_provenance"] if e["node_id"] == "s0")
    assert entry == {"node_id": "s0", "page": 1, "bounding_regions": _REGIONS_HEADING}


def test_document_node_uses_first_paragraph_with_regions() -> None:
    out = build_provenance(_GRAPH, _OCR)
    entry = next(e for e in out["node_provenance"] if e["node_id"] == "doc")
    assert entry == {"node_id": "doc", "page": 1, "bounding_regions": _REGIONS_TITLE}


def test_unmatched_node_is_omitted() -> None:
    graph = {
        "nodes": [{"id": "s9", "type": "section", "label": "Nonexistent Heading"}],
    }
    out = build_provenance(graph, _OCR)
    assert out["node_provenance"] == []


def test_matched_paragraph_with_empty_bounding_regions_is_omitted() -> None:
    ocr = {
        "paragraphs": [
            {
                "role": "sectionHeading",
                "content": "Directors' Report",
                "bounding_regions": [],
                "spans": [],
            },
        ],
    }
    graph = {"nodes": [{"id": "s0", "type": "section", "label": "Directors' Report"}]}
    out = build_provenance(graph, ocr)
    assert out["node_provenance"] == []


def test_empty_graph_yields_empty_node_provenance() -> None:
    assert build_provenance({}, _OCR) == {"schema_version": 1, "node_provenance": []}
    assert build_provenance({"nodes": []}, _OCR) == {"schema_version": 1, "node_provenance": []}


def test_ambiguous_shared_prefix_resolves_to_first_in_document_order() -> None:
    ocr = {
        "paragraphs": [
            {
                "content": "Revenue grew twelve percent.",
                "bounding_regions": [{"page_number": 3, "polygon": []}],
                "spans": [],
            },
            {
                "content": "Revenue grew twelve percent in the prior year too.",
                "bounding_regions": [{"page_number": 4, "polygon": []}],
                "spans": [],
            },
        ],
    }
    graph = {
        "nodes": [{"id": "s0p0", "type": "paragraph", "label": "Revenue grew twelve percent."}]
    }
    out = build_provenance(graph, ocr)
    assert out["node_provenance"] == [
        {"node_id": "s0p0", "page": 3, "bounding_regions": [{"page_number": 3, "polygon": []}]}
    ]


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
    graph = {"nodes": [{"id": "s0", "type": "section", "label": "Directors' Report"}]}
    out = build_provenance(graph, ocr)
    assert out == {"schema_version": 1, "node_provenance": []}
