from fdi.core.retrieval import chunk_rows_for_index


def test_builds_rows_with_shared_doc_key_and_version() -> None:
    chunks = [
        {"chunk_id": "s0", "heading": "Balance Sheet", "text": "Balance Sheet\nAssets 100",
         "section_type": "BALANCE_SHEET", "page": 3, "para_count": 1},
        {"chunk_id": "s1", "heading": "Notes", "text": "Notes\nNote 1",
         "section_type": "NOTES", "page": 7, "para_count": 1},
    ]
    rows = chunk_rows_for_index(doc_id="d1", doc_key="annual-report", version=2, chunks=chunks)
    assert rows == [
        {"id": "d1:s0", "doc_id": "d1", "chunk_id": "s0", "doc_key": "annual-report",
         "version": 2, "heading": "Balance Sheet", "text": "Balance Sheet\nAssets 100",
         "section_type": "BALANCE_SHEET", "page": 3},
        {"id": "d1:s1", "doc_id": "d1", "chunk_id": "s1", "doc_key": "annual-report",
         "version": 2, "heading": "Notes", "text": "Notes\nNote 1",
         "section_type": "NOTES", "page": 7},
    ]


def test_skips_record_with_non_str_or_empty_chunk_id() -> None:
    chunks = [
        {"chunk_id": 123, "heading": "A", "text": "A\n"},
        {"chunk_id": "", "heading": "B", "text": "B\n"},
        {"chunk_id": "s2", "heading": "C", "text": "C\n"},
    ]
    rows = chunk_rows_for_index(doc_id="d1", doc_key=None, version=None, chunks=chunks)
    assert [r["chunk_id"] for r in rows] == ["s2"]


def test_degrades_heading_section_type_page_text_to_defaults() -> None:
    chunks = [{"chunk_id": "s0", "heading": 1, "text": None, "section_type": 2, "page": "x"}]
    rows = chunk_rows_for_index(doc_id="d1", doc_key=None, version=None, chunks=chunks)
    assert rows == [
        {"id": "d1:s0", "doc_id": "d1", "chunk_id": "s0", "doc_key": None, "version": None,
         "heading": None, "text": "", "section_type": None, "page": None}
    ]


def test_empty_chunks_returns_empty_list() -> None:
    assert chunk_rows_for_index(doc_id="d1", doc_key=None, version=None, chunks=[]) == []
