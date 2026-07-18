from fdi.core.retrieval import build_chunks


def _ast(sections: list[object]) -> dict[str, object]:
    return {"schema_version": 1, "title": "T", "sections": sections}


def test_build_chunks_joins_text_type_page() -> None:
    ast = _ast([
        {"heading": "Balance Sheet", "paragraphs": ["Assets 100", "Liab 40"]},
        {"heading": "Notes", "paragraphs": ["Note 1"]},
    ])
    semantic = {"sections": [{"type": "BALANCE_SHEET"}, {"type": "NOTES"}]}
    provenance = {"node_provenance": [
        {"node_id": "s0", "page": 3}, {"node_id": "s1", "page": 7},
    ]}
    out = build_chunks(ast, semantic, provenance)
    assert out["schema_version"] == 1
    assert out["chunks"] == [
        {"chunk_id": "s0", "heading": "Balance Sheet",
         "text": "Balance Sheet\nAssets 100\nLiab 40",
         "section_type": "BALANCE_SHEET", "page": 3, "para_count": 2},
        {"chunk_id": "s1", "heading": "Notes",
         "text": "Notes\nNote 1", "section_type": "NOTES", "page": 7, "para_count": 1},
    ]


def test_text_matches_embedding_construction() -> None:
    # byte-identical to EmbeddingStage: heading + "\n" + "\n".join(paragraphs)
    heading, paragraphs = "H", ["a", "b"]
    ast = _ast([{"heading": heading, "paragraphs": paragraphs}])
    out = build_chunks(ast, {}, {})
    assert out["chunks"][0]["text"] == heading + "\n" + "\n".join(paragraphs)


def test_chunk_id_mirrors_enumerate_skip() -> None:
    # index 1 is a non-dict -> skipped, consumes the index; next real section is s2
    ast = _ast([
        {"heading": "A", "paragraphs": []},
        "not-a-dict",
        {"heading": "C", "paragraphs": []},
    ])
    out = build_chunks(ast, {}, {})
    assert [c["chunk_id"] for c in out["chunks"]] == ["s0", "s2"]


def test_section_type_degrades_to_none() -> None:
    ast = _ast([{"heading": "A", "paragraphs": []}, {"heading": "B", "paragraphs": []}])
    semantic = {"sections": [{"type": 123}]}  # non-str type, and only one entry
    out = build_chunks(ast, semantic, {})
    assert [c["section_type"] for c in out["chunks"]] == [None, None]


def test_page_degrades_to_none_when_provenance_missing() -> None:
    ast = _ast([{"heading": "A", "paragraphs": []}, {"heading": "B", "paragraphs": []}])
    provenance = {"node_provenance": [{"node_id": "s0", "page": 5}]}  # s1 absent
    out = build_chunks(ast, {}, provenance)
    assert [c["page"] for c in out["chunks"]] == [5, None]


def test_empty_inputs() -> None:
    assert build_chunks({}, {}, {}) == {"schema_version": 1, "chunks": []}


def test_none_heading_kept_as_none_but_text_uses_empty() -> None:
    ast = _ast([{"heading": None, "paragraphs": ["x"]}])
    out = build_chunks(ast, {}, {})
    assert out["chunks"][0]["heading"] is None
    assert out["chunks"][0]["text"] == "\nx"
