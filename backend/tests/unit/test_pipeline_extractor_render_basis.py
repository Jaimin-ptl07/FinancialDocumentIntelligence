"""TR-1 P3-wire + P4b: unit tests for the pure(ish) pipeline-layer helpers
`_basis_keep`, `_card_identity`, and `_render_table_candidate` -- no async
session/store/embedder needed, since each takes plain Mappings/dataclasses,
mirroring how tests/unit/test_table_render.py exercises fdi.core.table_render
directly."""

from fdi.core.extractor import Candidate
from fdi.pipeline.extractor import _basis_keep, _card_identity, _render_table_candidate


def _grid(
    headers: list[str], rows: list[list[str]], header_row_count: int = 1
) -> dict[str, object]:
    return {
        "n_rows": len(rows) + header_row_count,
        "n_cols": len(headers),
        "header_row_count": header_row_count,
        "column_headers": headers,
        "grid": [headers, *rows],
        "cell_regions": [],
        "cell_pages": [],
    }


def _table_card(
    *, doc_id: str = "doc1", table_index: int = 0,
    identity: str = "Balance Sheet | Standalone | page 91",
) -> Candidate:
    text = f"[{identity}]\nColumns: FY24 | FY23\nRows: Total assets"
    return Candidate(
        evidence_id=f"{doc_id}:t{table_index}", text=text, page=91, score=1.0,
        source_tool="hybrid",
    )


# ---------------------------------------------------------------------------
# _basis_keep
# ---------------------------------------------------------------------------


def test_basis_keep_standalone_preference_keeps_standalone_and_unknown() -> None:
    assert _basis_keep("standalone", "standalone") is True
    assert _basis_keep("unknown", "standalone") is True


def test_basis_keep_standalone_preference_drops_consolidated() -> None:
    assert _basis_keep("consolidated", "standalone") is False


def test_basis_keep_consolidated_preference_is_symmetric() -> None:
    assert _basis_keep("consolidated", "consolidated") is True
    assert _basis_keep("unknown", "consolidated") is True
    assert _basis_keep("standalone", "consolidated") is False


def test_basis_keep_both_preference_keeps_everything() -> None:
    assert _basis_keep("standalone", "both") is True
    assert _basis_keep("consolidated", "both") is True
    assert _basis_keep("unknown", "both") is True


# ---------------------------------------------------------------------------
# _card_identity
# ---------------------------------------------------------------------------


def test_card_identity_strips_brackets_from_first_line() -> None:
    text = "[Balance Sheet | Standalone | page 91]\nColumns: x\nRows: y"
    assert _card_identity(text) == "Balance Sheet | Standalone | page 91"


def test_card_identity_no_brackets_returns_first_line_verbatim() -> None:
    assert _card_identity("no brackets here\nmore text") == "no brackets here"


def test_card_identity_empty_text_never_raises() -> None:
    assert _card_identity("") == ""


# ---------------------------------------------------------------------------
# _render_table_candidate
# ---------------------------------------------------------------------------


def test_render_table_candidate_renders_markdown_for_table_card() -> None:
    structure = {
        "tables": [
            {"grid": _grid(
                ["Particulars", "FY24", "FY23"],
                [["Total assets", "16,427.37", "14,213.08"]],
            )},
        ],
    }
    card = _table_card()
    out = _render_table_candidate(card, structure=structure)
    assert out.text != card.text
    assert "| --- | --- | --- |" in out.text
    assert "Particulars" in out.text
    assert "Total assets" in out.text
    assert "16,427.37" in out.text
    # identity/metadata preserved -- only `text` changes
    assert out.evidence_id == card.evidence_id
    assert out.page == card.page
    assert out.score == card.score


def test_render_table_candidate_uses_card_identity_as_title() -> None:
    structure = {"tables": [{"grid": _grid(["A", "B"], [["1", "2"]])}]}
    card = _table_card(identity="Balance Sheet | Standalone | page 91")
    out = _render_table_candidate(card, structure=structure)
    assert out.text.startswith("Table: Balance Sheet | Standalone | page 91")


def test_render_table_candidate_out_of_range_index_leaves_text_unchanged() -> None:
    structure: dict[str, object] = {"tables": []}
    card = _table_card(table_index=5)
    out = _render_table_candidate(card, structure=structure)
    assert out.text == card.text


def test_render_table_candidate_missing_grid_leaves_text_unchanged() -> None:
    structure = {"tables": [{}]}
    card = _table_card()
    out = _render_table_candidate(card, structure=structure)
    assert out.text == card.text


def test_render_table_candidate_malformed_structure_never_raises() -> None:
    card = _table_card()
    assert _render_table_candidate(card, structure={}).text == card.text
    assert _render_table_candidate(card, structure={"tables": "nope"}).text == card.text
    assert _render_table_candidate(card, structure={"tables": [5]}).text == card.text


def test_render_table_candidate_section_chunk_is_unchanged() -> None:
    structure = {"tables": [{"grid": _grid(["A"], [["1"]])}]}
    section = Candidate(
        evidence_id="doc1:s0", text="Some narrative text", page=3, score=1.0,
        source_tool="hybrid",
    )
    out = _render_table_candidate(section, structure=structure)
    assert out is section


def test_render_table_candidate_evidence_id_without_colon_is_unchanged() -> None:
    structure = {"tables": [{"grid": _grid(["A"], [["1"]])}]}
    card = Candidate(evidence_id="t0", text="bare id, no doc prefix", page=1, score=1.0,
                      source_tool="hybrid")
    out = _render_table_candidate(card, structure=structure)
    assert out is card
