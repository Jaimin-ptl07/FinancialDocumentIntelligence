"""The chunk_id -> geometry join, and every place it is lossy.

`s{i}` resolves through the graph node of the same id into the `provenance`
artifact -- but that node's source_index is the section's HEADING paragraph
(core.graph.build_graph), so the box covers the heading, not the body. Table
chunks have no provenance node at all (build_graph gives t{i} nodes
source_index=None, and build_provenance omits those), so `t{i}`/`t{i}r{j}`
resolve through `structure` instead -- per-cell for a row, whole-table for a
table. Everything else is [].
"""

from fdi.core.provenance import resolve_chunk_regions, resolve_evidence_regions

HEAD = [{"page_number": 1, "polygon": [0, 0, 1, 0, 1, 1, 0, 1]}]
ROW = [{"page_number": 4, "polygon": [2, 2, 3, 2, 3, 3, 2, 3]}]
TABLE = [{"page_number": 4, "polygon": [0, 0, 9, 0, 9, 9, 0, 9]}]


def _provenance() -> dict[str, object]:
    return {
        "schema_version": 1,
        "node_provenance": [
            {"node_id": "doc", "page": 1, "bounding_regions": HEAD},
            {"node_id": "s0", "page": 1, "bounding_regions": HEAD},
            {"node_id": "s0p1", "page": 2, "bounding_regions": ROW},
        ],
    }


def _structure() -> dict[str, object]:
    return {
        "schema_version": 3,
        "tables": [
            {
                "bounding_regions": TABLE,
                "grid": {
                    "n_rows": 2,
                    "n_cols": 2,
                    "header_row_count": 1,
                    "column_headers": ["Particulars", "FY24"],
                    "grid": [["Particulars", "FY24"], ["Total assets", "12,345"]],
                    "cell_regions": [[[], []], [ROW, ROW]],
                    "cell_pages": [[None, None], [4, 4]],
                },
            },
        ],
    }


def test_section_chunk_resolves_to_its_heading_paragraph_box() -> None:
    assert resolve_chunk_regions("s0", _provenance(), _structure()) == HEAD


def test_section_chunk_with_no_provenance_entry_is_empty() -> None:
    """build_provenance OMITS a node whose heading paragraph has no usable
    bounding_regions -- a conservative policy this join must not paper over."""
    assert resolve_chunk_regions("s7", _provenance(), _structure()) == []


def test_table_row_chunk_resolves_to_that_row_s_cell_regions() -> None:
    """Deduplicated: both cells in row 1 carry the same region."""
    assert resolve_chunk_regions("t0r1", _provenance(), _structure()) == ROW


def test_table_row_chunk_without_cell_geometry_falls_back_to_the_table() -> None:
    structure = _structure()
    structure["tables"][0]["grid"]["cell_regions"] = [[[], []], [[], []]]
    assert resolve_chunk_regions("t0r1", _provenance(), structure) == TABLE


def test_whole_table_chunk_resolves_to_the_table_box() -> None:
    assert resolve_chunk_regions("t0", _provenance(), _structure()) == TABLE


def test_out_of_range_table_or_row_index_is_empty() -> None:
    assert resolve_chunk_regions("t9", _provenance(), _structure()) == []
    assert resolve_chunk_regions("t0r9", _provenance(), _structure()) == []


def test_paragraph_figure_and_garbage_ids_are_empty() -> None:
    """s{i}p{j} and f{k} nodes exist in the graph but are never chunk ids, so
    they never reach here from a real evidence_id; a hallucinated id must
    also degrade, not raise."""
    for chunk_id in ("s0p1", "f3", "", "nonsense", "s", "t", "trx", "s-1"):
        assert resolve_chunk_regions(chunk_id, _provenance(), _structure()) == []


def test_missing_artifacts_never_raise() -> None:
    for chunk_id in ("s0", "t0", "t0r1"):
        assert resolve_chunk_regions(chunk_id, {}, {}) == []
    assert resolve_chunk_regions("s0", {"node_provenance": "nope"}, {"tables": 7}) == []
    assert resolve_chunk_regions("t0", {}, {"tables": [None]}) == []
    assert resolve_chunk_regions("t0r1", {}, {"tables": [{"grid": "nope"}]}) == []


def test_resolve_evidence_regions_strips_the_doc_prefix_and_unions() -> None:
    out = resolve_evidence_regions(
        "d1", ["d1:s0", "d1:t0r1"], _provenance(), _structure()
    )
    assert out == HEAD + ROW


def test_resolve_evidence_regions_deduplicates_across_evidence_ids() -> None:
    """A field cited from both a row and its whole table must not paint two
    boxes for one answer -- but the row box and the table box are different,
    so only true repeats collapse."""
    out = resolve_evidence_regions(
        "d1", ["d1:t0r1", "d1:t0r1", "d1:t0"], _provenance(), _structure()
    )
    assert out == ROW + TABLE


def test_resolve_evidence_regions_tolerates_junk_ids() -> None:
    assert resolve_evidence_regions("d1", [], _provenance(), _structure()) == []
    assert resolve_evidence_regions("d1", [None, 7, "d1:"], _provenance(), _structure()) == []
    # A bare chunk id with no "{doc_id}:" prefix still resolves.
    assert resolve_evidence_regions("d1", ["s0"], _provenance(), _structure()) == HEAD
    # An id belonging to a DIFFERENT document is not this document's geometry.
    assert resolve_evidence_regions("d1", ["d2:s0"], _provenance(), _structure()) == []
