import pytest

from fdi.core.agent import AgentResponse, ToolCall, dedup_citations, extract_citations


def test_agent_response_requires_exactly_one_of_tool_calls_or_content() -> None:
    AgentResponse(content="ok")  # fine
    AgentResponse(tool_calls=[ToolCall(id="1", name="search", arguments={})])  # fine
    with pytest.raises(ValueError, match="exactly one"):
        AgentResponse()  # neither
    with pytest.raises(ValueError, match="exactly one"):
        AgentResponse(
            content="ok", tool_calls=[ToolCall(id="1", name="search", arguments={})]
        )  # both
    with pytest.raises(ValueError, match="exactly one"):
        AgentResponse(tool_calls=[])  # empty list counts as "not set"


def test_extract_citations_error_result_is_empty() -> None:
    assert extract_citations("search", {}, {"error": "boom"}) == []


def test_extract_citations_search() -> None:
    result = {"results": [
        {"doc_id": "d1", "doc_key": "k1", "version": 1, "chunk_id": "s0",
         "heading": "Balance Sheet", "page": 3, "section_type": "BALANCE_SHEET",
         "text": "...", "score": 0.9},
    ]}
    out = extract_citations("search", {"query": "q"}, result)
    assert out == [{
        "doc_id": "d1", "doc_key": "k1", "version": 1, "heading": "Balance Sheet",
        "page": 3, "chunk_id": "s0", "table_index": None, "row_label": None,
        "column_header": None, "node_id": None,
    }]


def test_extract_citations_document_structure() -> None:
    result = {
        "schema_version": 1, "doc_id": "d1",
        "document": {
            "id": "d1:doc", "type": "document", "label": "ACME",
            "citation": {"doc_id": "d1", "doc_key": "k1", "version": 1,
                         "heading": "ACME", "page": None, "bounding_regions": None},
        },
        "sections": [{
            "id": "d1:s0", "type": "section", "label": "Balance Sheet",
            "citation": {"doc_id": "d1", "doc_key": "k1", "version": 1,
                         "heading": "Balance Sheet", "page": 3, "bounding_regions": None},
            "paragraphs": [],
        }],
    }
    out = extract_citations("document_structure", {"doc_id": "d1"}, result)
    assert {"doc_id": "d1", "doc_key": "k1", "version": 1, "heading": "Balance Sheet",
            "page": 3, "chunk_id": None, "table_index": None, "row_label": None,
            "column_header": None, "node_id": "d1:s0"} in out
    assert len(out) == 2  # document node + one section, no paragraphs


def test_extract_citations_get_tables() -> None:
    result = {"tables": [{"table_index": 0, "row_count": 2, "column_count": 2,
                           "stitched": False, "grid": {}}]}
    out = extract_citations("get_tables", {"doc_id": "d1"}, result)
    assert out == [{
        "doc_id": "d1", "doc_key": None, "version": None, "heading": None, "page": None,
        "chunk_id": None, "table_index": 0, "row_label": None, "column_header": None,
        "node_id": None,
    }]


def test_extract_citations_concept_lookup() -> None:
    result = {"rows": [{
        "table_index": 0, "row_label": "Revenue",
        "values": [{"column_header": "FY2023", "raw": "100", "value": 100.0}],
    }]}
    out = extract_citations("concept_lookup", {"doc_id": "d1", "concept": "REVENUE"}, result)
    assert out == [{
        "doc_id": "d1", "doc_key": None, "version": None, "heading": None, "page": None,
        "chunk_id": None, "table_index": 0, "row_label": "Revenue",
        "column_header": "FY2023", "node_id": None,
    }]


def test_extract_citations_resolve_entities() -> None:
    result = {"entities": [{"members": [
        {"doc_id": "d1", "doc_key": "k1", "version": 1},
        {"doc_id": "d2", "doc_key": "k1", "version": 2},
    ]}]}
    out = extract_citations("resolve_entities", {}, result)
    assert len(out) == 2
    assert out[0]["doc_id"] == "d1" and out[1]["doc_id"] == "d2"


def test_extract_citations_entity_timeline() -> None:
    result = {"timeline": [{"doc_id": "d1", "doc_key": "k1", "version": 1}]}
    out = extract_citations("entity_timeline", {"doc_key": "k1"}, result)
    assert out == [{
        "doc_id": "d1", "doc_key": "k1", "version": 1, "heading": None, "page": None,
        "chunk_id": None, "table_index": None, "row_label": None, "column_header": None,
        "node_id": None,
    }]


def test_extract_citations_compare_concept_only_matched_rows() -> None:
    result = {"versions": [
        {"doc_id": "d1", "doc_key": "k1", "version": 1, "status": "matched",
         "rows": [{"table_index": 0, "row_label": "Revenue",
                    "values": [{"column_header": "FY2023", "raw": "100", "value": 100.0}]}]},
        {"doc_id": "d2", "doc_key": "k1", "version": 2, "status": "no_structure", "rows": []},
    ]}
    out = extract_citations(
        "compare_concept_across_versions", {"doc_key": "k1", "concept": "REVENUE"}, result
    )
    assert len(out) == 1
    assert out[0]["doc_id"] == "d1" and out[0]["column_header"] == "FY2023"


def test_dedup_citations_collapses_exact_duplicates_preserves_order() -> None:
    a = {"doc_id": "d1", "chunk_id": "s0"}
    b = {"doc_id": "d1", "chunk_id": "s1"}
    out = dedup_citations([a, dict(a), b, dict(a)])
    assert out == [a, b]
