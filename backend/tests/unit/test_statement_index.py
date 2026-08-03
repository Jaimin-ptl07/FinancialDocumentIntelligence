"""Find the face by CONTENT, per basis. Heading detection returned three false
positives on TCS and could not say WHICH statement it had found."""

from fdi.core.fog_build import build_statement_index


def _cell(r: int, c: int, text: str) -> dict[str, object]:
    return {"row_index": r, "column_index": c, "content": text}


def _pl_table(page: int, pbt: str = "30") -> dict[str, object]:
    return {
        "bounding_regions": [{"page_number": page}],
        "spans": [{"offset": page * 1000, "length": 100}],
        "cells": [
            _cell(0, 0, "Particulars"), _cell(0, 1, "Note"), _cell(0, 2, "2025-26"),
            _cell(1, 0, "Revenue from operations"), _cell(1, 1, "10"), _cell(1, 2, "90"),
            _cell(2, 0, "Total income"), _cell(2, 2, "100"),
            _cell(3, 0, "Total expenses"), _cell(3, 2, "70"),
            _cell(4, 0, "Profit before tax"), _cell(4, 2, pbt),
        ],
    }


_BASIS = {
    "default_basis": "unknown",
    "regions": [
        {"basis": "standalone", "start_page": 90, "end_page": 140},
        {"basis": "consolidated", "start_page": 141, "end_page": 200},
    ],
}


def test_both_bases_surface_and_are_typed():
    ocr = {"paragraphs": [], "tables": [_pl_table(100), _pl_table(150)]}
    index = build_statement_index(ocr, _BASIS)
    found = {(s["basis"], s["statement"]) for s in index["statements"]}
    assert ("standalone", "profit_and_loss") in found
    assert ("consolidated", "profit_and_loss") in found


def test_the_statement_id_is_stable_and_addressable():
    ocr = {"paragraphs": [], "tables": [_pl_table(150)]}
    index = build_statement_index(ocr, _BASIS)
    ids = [s["id"] for s in index["statements"]]
    assert "st:consolidated:profit_and_loss" in ids


def test_table_index_points_back_into_the_ocr_tables_list():
    ocr = {"paragraphs": [], "tables": [{"cells": []}, _pl_table(150)]}
    index = build_statement_index(ocr, _BASIS)
    stmt = next(s for s in index["statements"] if s["basis"] == "consolidated")
    assert stmt["table_index"] == 1


def test_footing_is_recorded_on_the_statement():
    ocr = {"paragraphs": [], "tables": [_pl_table(150)]}
    stmt = build_statement_index(ocr, _BASIS)["statements"][0]
    assert stmt["footing"]["passed"] is True


def test_a_non_footing_face_is_still_indexed_but_flagged():
    """A face that does not tie is a finding, not a reason to lose the statement."""
    ocr = {"paragraphs": [], "tables": [_pl_table(150, pbt="44")]}
    stmt = build_statement_index(ocr, _BASIS)["statements"][0]
    assert stmt["footing"]["passed"] is False
    assert stmt["page"] == 150


def test_the_best_scoring_table_wins_within_a_basis():
    """Two candidates in one region must collapse to one statement, not two."""
    ocr = {"paragraphs": [], "tables": [_pl_table(150), _pl_table(160)]}
    index = build_statement_index(ocr, _BASIS)
    pl = [s for s in index["statements"] if s["statement"] == "profit_and_loss"]
    assert len(pl) == 1


def test_a_document_with_no_basis_regions_still_indexes():
    """No anchors is a real shape; the whole document becomes one unknown region."""
    ocr = {"paragraphs": [], "tables": [_pl_table(10)]}
    index = build_statement_index(ocr, {"regions": [], "default_basis": "unknown"})
    assert index["statements"]
    assert index["statements"][0]["basis"] == "unknown"


def test_output_is_deterministic_for_content_addressing():
    ocr = {"paragraphs": [], "tables": [_pl_table(150), _pl_table(100)]}
    assert build_statement_index(ocr, _BASIS) == build_statement_index(ocr, _BASIS)


def test_malformed_input_never_raises():
    assert build_statement_index({}, {}) == {"schema_version": 1, "statements": []}
    assert build_statement_index({"tables": "nope"}, {})["statements"] == []


def test_the_stage_is_registered_with_the_right_deps():
    import fdi.pipeline.stages  # noqa: F401 - triggers registration
    from fdi.pipeline.registry import REGISTRY

    stage = REGISTRY.get("statement_index")
    assert stage.deps == ("ocr", "structure", "basis")
    assert stage.version == 1


def test_the_dag_still_resolves_with_the_new_stage():
    """An unknown dep or a cycle fails here, not at runtime on a real document."""
    import fdi.pipeline.stages  # noqa: F401
    from fdi.pipeline.registry import REGISTRY
    from fdi.pipeline.resolver import DagResolver

    resolver = DagResolver(REGISTRY.all())
    assert "statement_index" in resolver.topological_order()
